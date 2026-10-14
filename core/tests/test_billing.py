from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import pytest

from lasteverlm_core import (
    DEFAULT_PRICING_RULES,
    PRICING_RULES_TTL_SECONDS,
    PRICING_SETTINGS_KEY,
    PRICING_SQL,
    ActionPricing,
    ExchangeRateMissingError,
    PricingRules,
    PricingRulesCache,
    charge_for,
    estimate_charge,
)

from .conftest import FakeAsyncConnection, FakeConnection

RULES = {
    "usd_per_eur": 1.25,
    "default_margin_pct": 20,
    "actions": {
        "output:audio": {"margin_pct": 0, "fixed_cents": 5},
        "judge": {"margin_pct": 12.5},
        "source:pdf": {"fixed_cents": 0.5},
    },
}


def test_parse_reads_margins_and_fees() -> None:
    rules = PricingRules.parse(RULES)
    assert rules.margin_pct("chat_answer") == 20  # the default
    assert rules.margin_pct("output:audio") == 0  # its own, even 0
    assert rules.margin_pct("source:pdf") == 20  # a fee alone keeps the default margin
    assert rules.charge_cents("judge", Decimal(8)) == Decimal("9.000")
    assert rules.charge_cents("chat_answer", Decimal("0.33")) == Decimal("0.396")
    assert (rules.fixed_cents("output:audio"), rules.fixed_cents("judge")) == (
        Decimal(5),
        Decimal(0),
    )
    assert rules.fixed_cents("source:pdf") == Decimal("0.5")
    assert rules.as_json() == RULES
    assert PricingRules.parse(PricingRules.parse(RULES).as_json()) == rules
    plain = PricingRules.parse('{"usd_per_eur": 1.1188, "default_margin_pct": 0}')
    assert plain == PricingRules(usd_per_eur=Decimal("1.1188"))
    assert plain.charge_cents("judge", Decimal("0.1")) == Decimal("0.1")
    assert DEFAULT_PRICING_RULES.usd_per_eur is None  # no silent rate
    assert DEFAULT_PRICING_RULES.as_json()["usd_per_eur"] is None
    assert PRICING_SETTINGS_KEY == "pricing" and PRICING_RULES_TTL_SECONDS == 30


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "must be an object"),
        ({}, "need usd_per_eur"),
        ({"default_margin_pct": 5}, "need usd_per_eur"),
        ({"usd_per_eur": 0}, "usd_per_eur must be between 0.1 and 10"),
        ({"usd_per_eur": 11}, "usd_per_eur must be between"),
        ({"usd_per_eur": "1.1"}, "usd_per_eur must be a number"),
        ({"usd_per_eur": 1, "margin": 1}, "unknown fields margin"),
        (
            {"usd_per_eur": 1, "default_margin_pct": -1},
            "default_margin_pct must be between 0 and 1000",
        ),
        ({"usd_per_eur": 1, "default_margin_pct": 1001}, "between 0 and 1000"),
        ({"usd_per_eur": 1, "default_margin_pct": "5"}, "must be a number"),
        ({"usd_per_eur": 1, "default_margin_pct": True}, "must be a number"),
        ({"usd_per_eur": 1, "actions": []}, "actions must be an object"),
        ({"usd_per_eur": 1, "actions": {"podcast": {}}}, "unknown action 'podcast'"),
        ({"usd_per_eur": 1, "actions": {"unknown": {}}}, "unknown action 'unknown'"),
        ({"usd_per_eur": 1, "actions": {"judge": 5}}, "must be an object"),
        ({"usd_per_eur": 1, "actions": {"judge": {"fee": 5}}}, "unknown fields fee"),
        (
            {"usd_per_eur": 1, "actions": {"judge": {"fixed_cents": 100_001}}},
            "between 0 and 100000",
        ),
        (
            {"usd_per_eur": 1, "actions": {"judge": {"margin_pct": None, "fixed_cents": -1}}},
            "between 0",
        ),
    ],
)
def test_parse_refuses_what_it_cannot_book(value: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        PricingRules.parse(value)


def test_estimate_includes_the_fee_once(prices: Any) -> None:
    rules = PricingRules.parse(RULES)
    # 0.33 US cents, 0.264 euro cents at 1.25, plus 20 %
    assert estimate_charge(prices, rules, "chat_answer", "llm", 1000, 2000) == Decimal("0.3168")
    # 30 US cents, 24 euro cents at 0 margin, plus the 5-cent fee
    assert estimate_charge(prices, rules, "output:audio", "llm", 1_000_000) == 29
    with pytest.raises(ExchangeRateMissingError):
        estimate_charge(prices, DEFAULT_PRICING_RULES, "chat_answer", "llm", 1)
    with pytest.raises(ValueError, match="unknown action"):
        estimate_charge(prices, rules, "unknown", "llm", 1)


def test_charge_for_puts_an_observed_cost_through_the_current_rules() -> None:
    rules = PricingRules.parse(RULES)
    assert charge_for(rules, "chat_answer", Decimal("0.5")) == Decimal("0.6")  # 20 %
    # a whole audio output: margin 0, the 5-cent fee once, not per segment
    assert charge_for(rules, "output:audio", Decimal(24)) == 29
    assert charge_for(PricingRules(), "judge", Decimal("0.1")) == Decimal("0.1")  # no rate needed
    with pytest.raises(ValueError, match="unknown action"):
        charge_for(rules, "unknown", Decimal(1))


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_cache_reads_the_row_and_refreshes_after_the_ttl() -> None:
    clock = Clock()
    cache = PricingRulesCache(clock=clock)
    conn = FakeConnection(rows=[(RULES,), (None,)])
    assert cache.current(conn) == PricingRules.parse(RULES)
    assert conn.executed == [(PRICING_SQL, {"key": "pricing"})]
    clock.now = PRICING_RULES_TTL_SECONDS - 1
    assert cache.current(conn) == PricingRules.parse(RULES)  # cached
    clock.now = PRICING_RULES_TTL_SECONDS
    assert cache.current(conn) == DEFAULT_PRICING_RULES  # the row is gone
    assert len(conn.executed) == 2


def test_a_malformed_row_books_at_the_defaults_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    defaults = PricingRules(actions={"judge": ActionPricing(fixed_cents=Decimal(1))})
    cache = PricingRulesCache(defaults, clock=clock)
    bad = {"usd_per_eur": 1, "default_margin_pct": 5000}
    conn = FakeConnection(rows=[(bad,), (bad,), ('{"usd_per_eur": 1, "default_margin_pct": 1}',)])
    with caplog.at_level(logging.WARNING, logger="lasteverlm_core.billing"):
        for i in range(2):
            clock.now = i * PRICING_RULES_TTL_SECONDS
            assert cache.current(conn) == defaults
    assert len(caplog.records) == 1 and "between 0 and 1000" in caplog.text
    clock.now = 2 * PRICING_RULES_TTL_SECONDS
    assert cache.current(conn).default_margin_pct == 1


def test_a_failing_read_is_raised_not_hidden() -> None:
    class Broken(FakeConnection):
        def __init__(self) -> None:
            super().__init__()
            self._cursor.execute = self._fail  # type: ignore[method-assign]

        @staticmethod
        def _fail(*_: Any) -> None:
            raise RuntimeError("relation settings does not exist")

    with pytest.raises(RuntimeError, match="settings"):
        PricingRulesCache().current(Broken())


async def test_async_cache() -> None:
    conn = FakeAsyncConnection(rows=[({"usd_per_eur": 1, "default_margin_pct": 10},)])
    cache = PricingRulesCache()
    assert (await cache.acurrent(conn)).default_margin_pct == 10
    assert (await cache.acurrent(conn)).default_margin_pct == 10
    assert len(conn.executed) == 1
