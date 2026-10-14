"""Rate limiting, blocking and banning per client IP, with the access log.

The fine layer of the family's two-layer scheme (parent:
docs/architecture.md, "Rate limiting"); the coarse one is ingress-nginx.
Every API service loads `RateLimitMiddleware`. State lives in PostgreSQL,
in three tables of core's migration, so all replicas of all services
share one view of a client:

- `access_log`: one row per request (sampled for `/healthz`). The
  sliding window is a count over it.
- `ip_blocks`: over the limit means 429 for `BLOCK_MINUTES` and one
  strike; strikes reset after `STRIKE_WINDOW_HOURS` of quiet.
- `ip_bans`: the `BAN_AFTER_STRIKES`th strike within the window. 403 on
  everything until an admin lifts the ban, for at most `ban_max_days`
  (`BAN_MAX_DAYS`, editable at runtime like the other rules).

The rules are in `RateLimiter`, pure over a `RateLimitStore`; the
middleware only adapts ASGI to them. Raw IPs exist only here, for this
purpose, and `purge` removes them after `RETENTION_DAYS` (an active ban
after `ban_max_days`).
"""

from __future__ import annotations

import ipaddress
import itertools
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Literal, Protocol, cast
from uuid import UUID

import anyio

from .auth import AuthError, AuthSettings, current_user
from .settings import ConfigError, env_int
from .usage import AsyncConnection, Connection, scalar

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

UNKNOWN_IP = "0.0.0.0"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitSettings:
    """The limiter's env settings. The five rule values are the defaults
    that a stored `ratelimit` row overrides (`RateLimitRules`)."""

    per_minute: int = 120
    block_minutes: int = 15
    ban_after_strikes: int = 3
    strike_window_hours: int = 24
    healthz_sample: int = 100
    retention_days: int = 30
    ban_max_days: int = 90

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RateLimitSettings:
        """Read the env; a value outside its bounds is a `ConfigError` at startup."""
        settings = cls(
            per_minute=env_int("RATE_LIMIT_PER_MINUTE", cls.per_minute, env),
            block_minutes=env_int("BLOCK_MINUTES", cls.block_minutes, env),
            ban_after_strikes=env_int("BAN_AFTER_STRIKES", cls.ban_after_strikes, env),
            strike_window_hours=env_int("STRIKE_WINDOW_HOURS", cls.strike_window_hours, env),
            healthz_sample=env_int("ACCESS_LOG_HEALTHZ_SAMPLE", cls.healthz_sample, env),
            retention_days=env_int("RETENTION_DAYS", cls.retention_days, env),
            ban_max_days=env_int("BAN_MAX_DAYS", cls.ban_max_days, env),
        )
        # The same bounds the admin panel is held to, so an env value cannot
        # do what a stored row may not (0 per minute would block everyone).
        for name, (low, high) in RATE_LIMIT_RULE_BOUNDS.items():
            value = getattr(settings, name)
            if not low <= value <= high:
                raise ConfigError(f"{_ENV_NAMES[name]} must be between {low} and {high}")
        for name in ("healthz_sample", "retention_days"):
            if getattr(settings, name) < 1:
                raise ConfigError(f"{_ENV_NAMES[name]} must be at least 1")
        return settings


_ENV_NAMES = {
    "per_minute": "RATE_LIMIT_PER_MINUTE",
    "block_minutes": "BLOCK_MINUTES",
    "ban_after_strikes": "BAN_AFTER_STRIKES",
    "strike_window_hours": "STRIKE_WINDOW_HOURS",
    "healthz_sample": "ACCESS_LOG_HEALTHZ_SAMPLE",
    "retention_days": "RETENTION_DAYS",
    "ban_max_days": "BAN_MAX_DAYS",
}


# --- runtime rules ---------------------------------------------------------------
# The five rule values can be changed at runtime from the admin panel: one
# row in account's `settings` table, key `ratelimit`, a jsonb object with
# these integer fields within these bounds (inclusive). `ban_max_days` may
# be missing, as in a row stored before it became editable; it then comes
# from the defaults. Account's PUT validates with `RateLimitRules.parse`;
# the limiter re-reads the row at most every `RULES_TTL_SECONDS` per
# process. RETENTION_DAYS governs the purge only and stays env only.

RATE_LIMIT_SETTINGS_KEY = "ratelimit"
RULES_TTL_SECONDS = 30
RATE_LIMIT_RULE_BOUNDS: Mapping[str, tuple[int, int]] = MappingProxyType(
    {
        "per_minute": (1, 10_000),
        "block_minutes": (1, 1_440),
        "ban_after_strikes": (1, 100),
        "strike_window_hours": (1, 720),
        "ban_max_days": (1, 365),
    }
)
_OPTIONAL_RULES = frozenset({"ban_max_days"})
RULES_SQL = "select value from settings where key = %(key)s"


@dataclass(frozen=True)
class RateLimitRules:
    """The five runtime-editable rule values; field names are the jsonb keys."""

    per_minute: int
    block_minutes: int
    ban_after_strikes: int
    strike_window_hours: int
    ban_max_days: int = RateLimitSettings.ban_max_days

    @classmethod
    def from_settings(cls, settings: RateLimitSettings) -> RateLimitRules:
        return cls(**{name: getattr(settings, name) for name in RATE_LIMIT_RULE_BOUNDS})

    @classmethod
    def parse(cls, value: Any, defaults: RateLimitRules | None = None) -> RateLimitRules:
        """Validate a stored or submitted object; `ValueError` names what is wrong.

        A missing `ban_max_days` comes from `defaults` (the env settings'
        rules), else from `RateLimitSettings`' default."""
        if isinstance(value, str | bytes):
            value = json.loads(value)
        if not isinstance(value, Mapping):
            raise ValueError("rate-limit rules must be an object")
        required = set(RATE_LIMIT_RULE_BOUNDS) - _OPTIONAL_RULES
        if not required <= set(value) <= set(RATE_LIMIT_RULE_BOUNDS):
            expected = ", ".join(RATE_LIMIT_RULE_BOUNDS)
            optional = ", ".join(sorted(_OPTIONAL_RULES))
            raise ValueError(f"rate-limit rules need the fields {expected} ({optional} optional)")
        fallback = defaults or cls(1, 1, 1, 1)
        value = {name: value.get(name, getattr(fallback, name)) for name in RATE_LIMIT_RULE_BOUNDS}
        for name, (low, high) in RATE_LIMIT_RULE_BOUNDS.items():
            field_value = value[name]
            if isinstance(field_value, bool) or not isinstance(field_value, int):
                raise ValueError(f"{name} must be an integer")
            if not low <= field_value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
        return cls(**{name: value[name] for name in RATE_LIMIT_RULE_BOUNDS})

    def as_json(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in RATE_LIMIT_RULE_BOUNDS}


@dataclass(frozen=True)
class AccessLogEntry:
    """One `access_log` row: who called what, when, and with which status."""

    at: datetime
    ip: str
    path: str
    method: str
    status: int
    user_id: UUID | None
    service: str


@dataclass(frozen=True)
class Decision:
    """`allowed`, `blocked` (429, `retry_after` seconds) or `banned` (403)."""

    kind: Literal["allowed", "blocked", "banned"]
    retry_after: int = 0

    @property
    def status(self) -> int:
        return {"allowed": 200, "blocked": 429, "banned": 403}[self.kind]


ALLOWED = Decision("allowed")
BANNED = Decision("banned")


class RateLimitStore(Protocol):
    """What the rules need from storage. Implemented over PostgreSQL and in memory."""

    async def is_banned(self, ip: str, since: datetime) -> bool:
        """An unlifted ban on `ip` created at or after `since`; an older one has expired."""
        ...

    async def blocked_until(self, ip: str) -> datetime | None: ...
    async def requests_since(self, ip: str, since: datetime) -> int: ...
    async def strike(
        self, ip: str, blocked_until: datetime, now: datetime, window_start: datetime
    ) -> int:
        """Block `ip` until `blocked_until` and count a strike at `now`.

        Returns the strikes on record, restarted at one when the previous
        strike was before `window_start`.
        """
        ...

    async def ban(self, ip: str, reason: str, now: datetime) -> None: ...
    async def log(self, entry: AccessLogEntry) -> None: ...
    async def rule_overrides(self) -> Any:
        """The stored `ratelimit` settings value, or None without a row."""
        ...


class RateLimiter:
    """The block and ban rules over a store. One instance per process."""

    def __init__(self, store: RateLimitStore, settings: RateLimitSettings) -> None:
        self._store = store
        self._settings = settings
        self._healthz_counter = itertools.count()
        self._defaults = RateLimitRules.from_settings(settings)
        self._rules = self._defaults
        self._rules_read_at: datetime | None = None
        self._warned: str | None = None

    @property
    def settings(self) -> RateLimitSettings:
        """The env settings; the thresholds in force are `rules`."""
        return self._settings

    @property
    def rules(self) -> RateLimitRules:
        return self._rules

    async def current_rules(self, now: datetime) -> RateLimitRules:
        """The stored rules, re-read at most every `RULES_TTL_SECONDS`; the env
        defaults without a row, and with one warning when the row is unusable."""
        read_at = self._rules_read_at
        if read_at is not None and timedelta(0) <= now - read_at < _RULES_TTL:
            return self._rules
        self._rules_read_at = now  # concurrent requests keep the old rules meanwhile
        try:
            raw = await self._store.rule_overrides()
            self._rules = (
                self._defaults if raw is None else RateLimitRules.parse(raw, self._defaults)
            )
            self._warned = None
        except Exception as exc:  # a malformed row or an unreadable table
            problem = f"{type(exc).__name__}: {exc}"
            if problem != self._warned:
                log.warning("rate-limit rules unusable, using env defaults: %s", problem)
                self._warned = problem
            self._rules = self._defaults
        return self._rules

    async def check(self, ip: str, now: datetime) -> Decision:
        s = await self.current_rules(now)
        if await self._store.is_banned(ip, now - timedelta(days=s.ban_max_days)):
            return BANNED
        until = await self._store.blocked_until(ip)
        if until is not None and until > now:
            return Decision("blocked", _retry_after(until, now))
        if await self._store.requests_since(ip, now - timedelta(minutes=1)) < s.per_minute:
            return ALLOWED
        until = now + timedelta(minutes=s.block_minutes)
        window_start = now - timedelta(hours=s.strike_window_hours)
        strikes = await self._store.strike(ip, until, now, window_start)
        if strikes >= s.ban_after_strikes:
            reason = f"{strikes} rate-limit strikes within {s.strike_window_hours}h"
            await self._store.ban(ip, reason, now)
            return BANNED
        return Decision("blocked", _retry_after(until, now))

    async def record(self, entry: AccessLogEntry) -> None:
        """Write the access-log row; `/healthz` rows are sampled 1 in `healthz_sample`."""
        if entry.path.endswith("/healthz") and (
            next(self._healthz_counter) % max(self._settings.healthz_sample, 1)
        ):
            return
        await self._store.log(entry)


_RULES_TTL = timedelta(seconds=RULES_TTL_SECONDS)


def _retry_after(until: datetime, now: datetime) -> int:
    return max(1, int((until - now).total_seconds() + 0.999))


def client_ip(scope: Scope, trust_forwarded: bool = True) -> str:
    """The client address: the last `X-Forwarded-For` entry when trusted, else the peer.

    The last entry is the one the trusted proxy (the ingress) wrote; every
    entry before it came from the client and can be forged, so it is never
    read. A service reachable other than through that one proxy (local
    runs) passes `trust_forwarded=False` (account's `TRUST_PROXY_HEADERS`). Anything that
    is not an IP address becomes `UNKNOWN_IP`, so a malformed header
    cannot break the request or the `inet` column. An IPv4-mapped IPv6
    address counts as its IPv4 address, and an IPv6 zone (`%eth0`) is
    dropped: one client, one key, and nothing `inet` would reject.
    """
    candidate: str | None = None
    if trust_forwarded:
        # Repeated header lines are one list in order (RFC 9110, 5.3).
        entries = [
            entry.strip()
            for name, value in scope.get("headers", ())
            if name == b"x-forwarded-for"
            for entry in value.decode("latin-1").split(",")
        ]
        entries = [entry for entry in entries if entry]
        if entries:
            candidate = entries[-1]
    if candidate is None:
        client = scope.get("client")
        candidate = client[0] if client else None
    try:
        address = ipaddress.ip_address(candidate or "")
    except ValueError:
        return UNKNOWN_IP
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        if address.scope_id is not None:
            return str(ipaddress.IPv6Address(str(address).split("%", 1)[0]))
    return str(address)


IPV6_PREFIX = 64


def limit_key(ip: str) -> str:
    """The key the rules count, block and ban under: an IPv4 address as is,
    an IPv6 address as its /64 network (`2001:db8:1:2::/64`).

    One IPv6 subscriber usually holds a whole /64 and can pick any address
    in it, so a per-address key would let it step out of every block and
    ban. `ip` is what `client_ip` returned.
    """
    address = ipaddress.ip_address(ip)
    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    return str(ipaddress.IPv6Network((address, IPV6_PREFIX), strict=False))


class RateLimitMiddleware:
    """Pure ASGI middleware: `check` before the app, `record` after it.

    The client is keyed with `limit_key` for the rules and the log alike.
    `service` names the writer in `access_log`. With `auth`, the row
    carries the user the bearer token resolves to; an invalid token is
    logged as anonymous and left for the app to reject.
    """

    def __init__(
        self,
        app: ASGIApp,
        limiter: RateLimiter,
        *,
        service: str,
        auth: AuthSettings | None = None,
        trust_forwarded: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._app = app
        self._limiter = limiter
        self._service = service
        self._auth = auth
        self._trust_forwarded = trust_forwarded
        self._clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        now = self._clock()
        ip = limit_key(client_ip(scope, self._trust_forwarded))
        decision = await self._limiter.check(ip, now)
        status = decision.status
        if decision.kind == "allowed":

            async def send_and_note(message: Message) -> None:
                nonlocal status
                if message["type"] == "http.response.start":
                    status = int(message["status"])
                await send(message)

            try:
                await self._app(scope, receive, send_and_note)
            except Exception:
                status = 500
                raise
            finally:
                await self._record(scope, ip, status, now)
        else:
            await _send_json(send, decision)
            await self._record(scope, ip, status, now)

    async def _record(self, scope: Scope, ip: str, status: int, now: datetime) -> None:
        await self._limiter.record(
            AccessLogEntry(
                at=now,
                ip=ip,
                path=scope.get("path", ""),
                method=scope.get("method", ""),
                status=status,
                user_id=self._user_id(scope),
                service=self._service,
            )
        )

    def _user_id(self, scope: Scope) -> UUID | None:
        if self._auth is None:
            return None
        header = next(
            (v.decode("latin-1") for k, v in scope.get("headers", ()) if k == b"authorization"),
            None,
        )
        try:
            return current_user(header, self._auth).user_id
        except AuthError:
            return None


async def _send_json(send: Send, decision: Decision) -> None:
    body = json.dumps(
        {"error": "too many requests" if decision.kind == "blocked" else "banned"}
    ).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if decision.kind == "blocked":
        headers.append((b"retry-after", str(decision.retry_after).encode()))
    await send({"type": "http.response.start", "status": decision.status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


# --- stores -----------------------------------------------------------------

# A ban older than ban_max_days has expired even before the purge removes it.
IS_BANNED_SQL = """
select 1 as banned from ip_bans
where ip = %(ip)s and lifted_at is null and created_at >= %(since)s
"""
BLOCKED_UNTIL_SQL = "select blocked_until from ip_blocks where ip = %(ip)s"
# 429s are the limiter's own answers and must not keep a client over the limit.
REQUESTS_SINCE_SQL = """
select count(*) as requests from access_log
where ip = %(ip)s and at >= %(since)s and status <> 429
"""
STRIKE_SQL = """
insert into ip_blocks (ip, blocked_until, strikes, updated_at)
values (%(ip)s, %(blocked_until)s, 1, %(now)s)
on conflict (ip) do update set
    blocked_until = excluded.blocked_until,
    strikes = case when ip_blocks.updated_at < %(window_start)s then 1
                   else ip_blocks.strikes + 1 end,
    updated_at = excluded.updated_at
returning strikes
"""
BAN_SQL = """
insert into ip_bans (ip, reason, created_at) values (%(ip)s, %(reason)s, %(now)s)
on conflict (ip) do update
set reason = excluded.reason, created_at = excluded.created_at, lifted_at = null
"""
LIFT_BAN_SQL = "update ip_bans set lifted_at = now() where ip = %(ip)s and lifted_at is null"
ACCESS_LOG_SQL = """
insert into access_log (at, ip, path, method, status, user_id, service)
values (%(at)s, %(ip)s, %(path)s, %(method)s, %(status)s, %(user_id)s, %(service)s)
"""
# Raw IPs are kept for the security purpose only. Everything older than
# `cutoff` goes, except an active ban: it stays until an admin lifts it, but
# no longer than `ban_cutoff`. Every statement takes `purge_params(...)`.
PURGE_SQL = (
    "delete from access_log where at < %(cutoff)s",
    "delete from ip_blocks where updated_at < %(cutoff)s",
    "delete from ip_bans"
    " where (lifted_at is not null and lifted_at < %(cutoff)s) or created_at < %(ban_cutoff)s",
)


def purge_params(
    retention_days: int = 30, ban_max_days: int = 90, now: datetime | None = None
) -> dict[str, datetime]:
    """The parameters of every `PURGE_SQL` statement: `cutoff` and `ban_cutoff`."""
    now = now or datetime.now(UTC)
    return {
        "cutoff": now - timedelta(days=retention_days),
        "ban_cutoff": now - timedelta(days=ban_max_days),
    }


def purge(
    conn: Connection,
    retention_days: int = 30,
    ban_max_days: int = 90,
    now: datetime | None = None,
) -> None:
    """Remove rows older than `retention_days` from the three tables, and active
    bans older than `ban_max_days`. The caller commits."""
    params = purge_params(retention_days, ban_max_days, now)
    with conn.cursor() as cur:
        for statement in PURGE_SQL:
            cur.execute(statement, params)


def lift_ban(conn: Connection, ip: str) -> None:
    """Lift an active ban; the admin panel's action. The caller commits."""
    with conn.cursor() as cur:
        cur.execute(LIFT_BAN_SQL, {"ip": ip})


def _strike_params(
    ip: str, blocked_until: datetime, now: datetime, window_start: datetime
) -> dict[str, Any]:
    return {"ip": ip, "blocked_until": blocked_until, "now": now, "window_start": window_start}


class PostgresRateLimitStore:
    """Over a sync psycopg connection source, e.g. `ConnectionPool.connection`.

    Each call takes one connection, runs one statement and commits; the
    blocking driver runs in a worker thread so the event loop stays free.
    """

    def __init__(self, connect: Callable[[], AbstractContextManager[Connection]]) -> None:
        self._connect = connect

    def _run(self, sql: str, params: Mapping[str, Any], fetch: bool) -> Any:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone() if fetch else None

    async def _query(self, sql: str, params: Mapping[str, Any], fetch: bool) -> Any:
        return await anyio.to_thread.run_sync(self._run, sql, params, fetch)

    async def is_banned(self, ip: str, since: datetime) -> bool:
        return await self._query(IS_BANNED_SQL, {"ip": ip, "since": since}, True) is not None

    async def blocked_until(self, ip: str) -> datetime | None:
        row = await self._query(BLOCKED_UNTIL_SQL, {"ip": ip}, True)
        return cast(datetime | None, scalar(row, "blocked_until"))

    async def requests_since(self, ip: str, since: datetime) -> int:
        row = await self._query(REQUESTS_SINCE_SQL, {"ip": ip, "since": since}, True)
        return int(scalar(row, "requests") or 0)

    async def rule_overrides(self) -> Any:
        row = await self._query(RULES_SQL, {"key": RATE_LIMIT_SETTINGS_KEY}, True)
        return scalar(row, "value")

    async def strike(
        self, ip: str, blocked_until: datetime, now: datetime, window_start: datetime
    ) -> int:
        params = _strike_params(ip, blocked_until, now, window_start)
        row = await self._query(STRIKE_SQL, params, True)
        return int(scalar(row, "strikes") or 1)

    async def ban(self, ip: str, reason: str, now: datetime) -> None:
        await self._query(BAN_SQL, {"ip": ip, "reason": reason, "now": now}, False)

    async def log(self, entry: AccessLogEntry) -> None:
        await self._query(ACCESS_LOG_SQL, entry.__dict__, False)


class AsyncPostgresRateLimitStore:
    """Over an async psycopg connection source, e.g. `AsyncConnectionPool.connection`."""

    def __init__(self, connect: Callable[[], AbstractAsyncContextManager[AsyncConnection]]) -> None:
        self._connect = connect

    async def _query(self, sql: str, params: Mapping[str, Any], fetch: bool) -> Any:
        async with self._connect() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchone() if fetch else None

    async def is_banned(self, ip: str, since: datetime) -> bool:
        return await self._query(IS_BANNED_SQL, {"ip": ip, "since": since}, True) is not None

    async def blocked_until(self, ip: str) -> datetime | None:
        row = await self._query(BLOCKED_UNTIL_SQL, {"ip": ip}, True)
        return cast(datetime | None, scalar(row, "blocked_until"))

    async def requests_since(self, ip: str, since: datetime) -> int:
        row = await self._query(REQUESTS_SINCE_SQL, {"ip": ip, "since": since}, True)
        return int(scalar(row, "requests") or 0)

    async def rule_overrides(self) -> Any:
        row = await self._query(RULES_SQL, {"key": RATE_LIMIT_SETTINGS_KEY}, True)
        return scalar(row, "value")

    async def strike(
        self, ip: str, blocked_until: datetime, now: datetime, window_start: datetime
    ) -> int:
        params = _strike_params(ip, blocked_until, now, window_start)
        row = await self._query(STRIKE_SQL, params, True)
        return int(scalar(row, "strikes") or 1)

    async def ban(self, ip: str, reason: str, now: datetime) -> None:
        await self._query(BAN_SQL, {"ip": ip, "reason": reason, "now": now}, False)

    async def log(self, entry: AccessLogEntry) -> None:
        await self._query(ACCESS_LOG_SQL, entry.__dict__, False)


@dataclass
class MemoryRateLimitStore:
    """In memory, per process: for tests and local runs without PostgreSQL."""

    entries: list[AccessLogEntry] = field(default_factory=list)
    # ip -> (blocked_until, strikes, last strike at)
    blocks: dict[str, tuple[datetime, int, datetime]] = field(default_factory=dict)
    bans: dict[str, str] = field(default_factory=dict)
    # ip -> created_at; a ban without an entry (set directly in a test) counts as current
    banned_at: dict[str, datetime] = field(default_factory=dict)
    # the `ratelimit` settings value as account would store it; None = no row
    overrides: Any = None

    async def rule_overrides(self) -> Any:
        return self.overrides

    async def is_banned(self, ip: str, since: datetime) -> bool:
        return ip in self.bans and self.banned_at.get(ip, since) >= since

    async def blocked_until(self, ip: str) -> datetime | None:
        block = self.blocks.get(ip)
        return block[0] if block else None

    async def requests_since(self, ip: str, since: datetime) -> int:
        return sum(1 for e in self.entries if e.ip == ip and e.at >= since and e.status != 429)

    async def strike(
        self, ip: str, blocked_until: datetime, now: datetime, window_start: datetime
    ) -> int:
        previous = self.blocks.get(ip)
        strikes = 1 if previous is None or previous[2] < window_start else previous[1] + 1
        self.blocks[ip] = (blocked_until, strikes, now)
        return strikes

    async def ban(self, ip: str, reason: str, now: datetime) -> None:
        self.bans[ip] = reason
        self.banned_at[ip] = now

    async def log(self, entry: AccessLogEntry) -> None:
        self.entries.append(entry)
