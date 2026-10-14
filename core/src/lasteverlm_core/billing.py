"""What a user pays: the pricing rules over the real cost of a call.

`cost_cents` (pricing.py) is what a model call really cost, in US cents,
as the platform prices it. Credits and charges are euro cents: the cost
is converted at the rules' `usd_per_eur` (dollars per euro, the way the
ECB quotes it), and the charge is that euro cost times
`1 + margin_pct / 100`, the margin of the action the call
belongs to or else the default margin; an action may also carry a fixed
fee, booked once per user action as its own `usage` row (kind "fee").
The dollar cost, the rate, the euro cost and the charge are fixed when a
row is booked (`record_usage`), so a later change of the rules or the
rate never rewrites what a past action cost or charged.

The rules are one row in account's `settings` table, key `pricing`:

    {"usd_per_eur": 1.1188, "default_margin_pct": 0,
     "actions": {"<action>": {"margin_pct": 20, "fixed_cents": 5}}}

`usd_per_eur` is required and has no default: a booking without a rate
fails (`ExchangeRateMissingError`) instead of charging dollars as euros.

Account's admin endpoint writes it, validated by `PricingRules.parse`;
every booking process reads it through a `PricingRulesCache`, at most
every `PRICING_RULES_TTL_SECONDS`. Without a row there is no rate, so
nothing can be booked; account's migration seeds the row.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from .models import USAGE_ACTIONS, is_usage_action
from .pricing import ModelPrices, cost_cents
from .usage import AsyncConnection, Connection, scalar

log = logging.getLogger(__name__)

PRICING_SETTINGS_KEY = "pricing"
PRICING_RULES_TTL_SECONDS = 30
MAX_MARGIN_PCT = Decimal(1000)
MAX_FIXED_CENTS = Decimal(100_000)
MIN_USD_PER_EUR = Decimal("0.1")
MAX_USD_PER_EUR = Decimal(10)
PRICING_SQL = "select value as value from settings where key = %(key)s"

_HUNDRED = Decimal(100)
# Euro cents to ten decimal places: a thousandth of a cent is still exact,
# and a non-terminating division by the rate gets a fixed width.
_EUR_PLACES = Decimal("1e-10")


class ExchangeRateMissingError(RuntimeError):
    """A booking needed `usd_per_eur` and the pricing rules have none."""

    def __init__(self) -> None:
        super().__init__(
            "no usd_per_eur in the pricing rules (settings row 'pricing'); "
            "a cost in US cents cannot be charged in euro cents without it"
        )


@dataclass(frozen=True)
class ActionPricing:
    """An action's own margin (None: the default applies) and fixed fee."""

    margin_pct: Decimal | None = None
    fixed_cents: Decimal = Decimal(0)


@dataclass(frozen=True)
class PricingRules:
    """The exchange rate, the default margin and the per-action margins and fees."""

    usd_per_eur: Decimal | None = None
    default_margin_pct: Decimal = Decimal(0)
    actions: Mapping[str, ActionPricing] = field(default_factory=lambda: MappingProxyType({}))

    def cost_eur_cents(self, cost_usd_cents: Decimal) -> Decimal:
        """A cost in US cents in euro cents at `usd_per_eur`; raises without a rate."""
        if self.usd_per_eur is None:
            raise ExchangeRateMissingError()
        return (cost_usd_cents / self.usd_per_eur).quantize(_EUR_PLACES)

    def margin_pct(self, action: str) -> Decimal:
        own = self.actions.get(action)
        if own is not None and own.margin_pct is not None:
            return own.margin_pct
        return self.default_margin_pct

    def charge_cents(self, action: str, cost: Decimal) -> Decimal:
        """`cost * (1 + margin / 100)`, exact; `cost` in euro cents."""
        return cost * (1 + self.margin_pct(action) / _HUNDRED)

    def fixed_cents(self, action: str) -> Decimal:
        own = self.actions.get(action)
        return own.fixed_cents if own is not None else Decimal(0)

    @classmethod
    def parse(cls, value: Any) -> PricingRules:
        """Validate a stored or submitted object; `ValueError` names what is wrong."""
        if isinstance(value, str | bytes):
            value = json.loads(value)
        if not isinstance(value, Mapping):
            raise ValueError("pricing rules must be an object")
        unknown = set(value) - {"usd_per_eur", "default_margin_pct", "actions"}
        if unknown:
            raise ValueError(f"pricing rules have unknown fields {', '.join(sorted(unknown))}")
        if "usd_per_eur" not in value:
            raise ValueError("pricing rules need usd_per_eur (US dollars per euro)")
        rate = _number(value["usd_per_eur"], "usd_per_eur", MAX_USD_PER_EUR, MIN_USD_PER_EUR)
        default = _number(value.get("default_margin_pct", 0), "default_margin_pct", MAX_MARGIN_PCT)
        raw_actions = value.get("actions", {})
        if not isinstance(raw_actions, Mapping):
            raise ValueError("actions must be an object keyed by action")
        actions: dict[str, ActionPricing] = {}
        for action, entry in raw_actions.items():
            if not is_usage_action(action):
                raise ValueError(f"unknown action {action!r}")
            if not isinstance(entry, Mapping):
                raise ValueError(f"actions[{action!r}] must be an object")
            extra = set(entry) - {"margin_pct", "fixed_cents"}
            if extra:
                raise ValueError(
                    f"actions[{action!r}] has unknown fields {', '.join(sorted(extra))}"
                )
            margin = entry.get("margin_pct")
            actions[action] = ActionPricing(
                margin_pct=(
                    None
                    if margin is None
                    else _number(margin, f"actions[{action!r}].margin_pct", MAX_MARGIN_PCT)
                ),
                fixed_cents=_number(
                    entry.get("fixed_cents", 0),
                    f"actions[{action!r}].fixed_cents",
                    MAX_FIXED_CENTS,
                ),
            )
        return cls(usd_per_eur=rate, default_margin_pct=default, actions=MappingProxyType(actions))

    def as_json(self) -> dict[str, Any]:
        actions: dict[str, dict[str, int | float]] = {}
        for action, own in sorted(self.actions.items()):
            entry: dict[str, int | float] = {}
            if own.margin_pct is not None:
                entry["margin_pct"] = _plain(own.margin_pct)
            if own.fixed_cents:
                entry["fixed_cents"] = _plain(own.fixed_cents)
            actions[action] = entry
        return {
            "usd_per_eur": None if self.usd_per_eur is None else _plain(self.usd_per_eur),
            "default_margin_pct": _plain(self.default_margin_pct),
            "actions": actions,
        }


DEFAULT_PRICING_RULES = PricingRules()


def _number(value: Any, name: str, high: Decimal, low: Decimal = Decimal(0)) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number")
    number = Decimal(str(value))
    if not low <= number <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return number


def _plain(number: Decimal) -> int | float:
    return int(number) if number == number.to_integral_value() else float(number)


def estimate_charge(
    prices: ModelPrices,
    rules: PricingRules,
    action: str,
    model: str,
    tokens_in: int,
    tokens_out: int = 0,
) -> Decimal:
    """What one call of `action` would charge in euro cents, its fixed fee included.

    Units as for `record_usage`: tokens, or seconds for `tts` and `stt`.
    Raises `ExchangeRateMissingError` when the rules have no rate.
    For an action of several calls, the caller adds the calls' charges
    and counts the fee once."""
    if not is_usage_action(action):
        raise ValueError(f"unknown action {action!r}; known: {', '.join(sorted(USAGE_ACTIONS))}")
    cost = rules.cost_eur_cents(cost_cents(prices, model, tokens_in, tokens_out))
    return charge_for(rules, action, cost)


def charge_for(rules: PricingRules, action: str, cost_eur_cents: Decimal) -> Decimal:
    """What one whole action costing `cost_eur_cents` charges under `rules`:
    the margin on the cost plus the action's fixed fee, once.

    For a price list: the observed euro cost of past actions (the sum of
    an action_id's `cost_cents`) through the current rules, so a changed
    margin shows at once instead of when old charges age out."""
    if not is_usage_action(action):
        raise ValueError(f"unknown action {action!r}; known: {', '.join(sorted(USAGE_ACTIONS))}")
    return rules.charge_cents(action, cost_eur_cents) + rules.fixed_cents(action)


class PricingRulesCache:
    """The stored pricing rules, re-read at most every `ttl` seconds.

    No row means `defaults`; a malformed row means `defaults` too, with one
    warning per distinct problem. The shipped defaults have no rate, so
    either stops every booking with `ExchangeRateMissingError` rather than
    charging at a rate nobody set. A failing read is raised: the read runs
    on the caller's connection, often inside the booking's transaction,
    which PostgreSQL has aborted by then."""

    def __init__(
        self,
        defaults: PricingRules = DEFAULT_PRICING_RULES,
        ttl: float = PRICING_RULES_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._defaults = defaults
        self._ttl = ttl
        self._clock = clock
        self._rules = defaults
        self._read_at: float | None = None
        self._warned: str | None = None

    def _fresh(self) -> bool:
        return self._read_at is not None and 0 <= self._clock() - self._read_at < self._ttl

    def _apply(self, raw: Any) -> PricingRules:
        self._read_at = self._clock()
        try:
            self._rules = self._defaults if raw is None else PricingRules.parse(raw)
            self._warned = None
        except ValueError as exc:  # json.JSONDecodeError is one too
            problem = str(exc)
            if problem != self._warned:
                log.warning("pricing rules unusable, using defaults: %s", problem)
                self._warned = problem
            self._rules = self._defaults
        return self._rules

    def current(self, conn: Connection) -> PricingRules:
        if self._fresh():
            return self._rules
        with conn.cursor() as cur:
            cur.execute(PRICING_SQL, {"key": PRICING_SETTINGS_KEY})
            return self._apply(scalar(cur.fetchone(), "value"))

    async def acurrent(self, conn: AsyncConnection) -> PricingRules:
        if self._fresh():
            return self._rules
        async with conn.cursor() as cur:
            await cur.execute(PRICING_SQL, {"key": PRICING_SETTINGS_KEY})
            return self._apply(scalar(await cur.fetchone(), "value"))
