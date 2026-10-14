from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from lasteverlm_core import (
    BALANCE_SQL,
    FEE_SQL,
    HAS_CREDIT_SQL,
    USAGE_ACTION_UNITS,
    USAGE_ACTIONS,
    USAGE_SQL,
    ExchangeRateMissingError,
    PricingRules,
    UnpricedModelError,
    Usage,
    abalance_cents,
    arecord_usage,
    balance_cents,
    balance_sql,
    funded_sql,
    is_usage_action,
    record_usage,
)

from .conftest import FakeAsyncConnection, FakeConnection

FREE = PricingRules(usd_per_eur=Decimal(1))  # one to one: the numbers stay readable
LLM = Usage("llm", "llm", 1000, 2000)


def test_balance_reads_one_row() -> None:
    user = uuid4()
    conn = FakeConnection(rows=[(250,)])
    assert balance_cents(conn, user) == 250
    sql, params = conn.executed[0]
    assert sql == BALANCE_SQL
    assert params == {"user_id": user}


def test_balance_is_exact_and_zero_without_rows() -> None:
    assert balance_cents(FakeConnection(rows=[(Decimal("12.345"),)]), uuid4()) == Decimal("12.345")
    assert balance_cents(FakeConnection(rows=[None]), uuid4()) == 0
    assert balance_cents(FakeConnection(rows=[(None,)]), uuid4()) == 0


def test_balance_counts_charges_not_costs() -> None:
    assert "coalesce(charge_cents, cost_cents)" in BALANCE_SQL
    assert "coalesce(charge_cents, cost_cents)" in HAS_CREDIT_SQL
    assert funded_sql("l.owner") == f"({balance_sql('l.owner')} > 0)"
    assert "user_id = l.owner" in balance_sql("l.owner")
    for bad in ("l.owner; drop table usage", "1", "a.b.c", "owner)", ""):
        with pytest.raises(ValueError, match="not a column reference"):
            balance_sql(bad)


def test_actions_cover_every_output_and_uploaded_source_kind() -> None:
    assert {"output:report", "output:chart", "source:audio", "chat_answer", "judge"} <= (
        USAGE_ACTIONS
    )
    assert "source:generated" not in USAGE_ACTIONS
    assert not is_usage_action("unknown")


def test_every_action_has_a_unit() -> None:
    assert set(USAGE_ACTION_UNITS) == USAGE_ACTIONS
    seconds = {a for a, u in USAGE_ACTION_UNITS.items() if u.measure == "seconds"}
    assert seconds == {"output:audio", "source:audio"}
    assert {USAGE_ACTION_UNITS[a].unit for a in seconds} == {"audio_minute"}
    assert USAGE_ACTION_UNITS["chat_answer"].unit == "question"
    assert USAGE_ACTION_UNITS["output:report"].unit == "output"
    assert USAGE_ACTION_UNITS["source:pdf"].unit == "source"  # per source, not per page
    assert USAGE_ACTION_UNITS["judge"].unit == "check"


def test_record_usage_prices_charges_and_inserts(prices: Any) -> None:
    user, notebook, action_id = uuid4(), uuid4(), uuid4()
    conn = FakeConnection()
    rules = PricingRules.parse({"usd_per_eur": 1, "default_margin_pct": 50})
    row = record_usage(
        conn, user, notebook, "rag", "chat_answer", action_id, LLM, prices=prices, rules=rules
    )
    assert row.cost_cents == Decimal("0.33")
    assert row.charge_cents == Decimal("0.495")
    assert (row.action, row.action_id, row.kind, row.model) == (
        "chat_answer",
        action_id,
        "llm",
        "llm",
    )
    [(sql, params)] = conn.executed
    assert sql == USAGE_SQL
    assert params == row.model_dump()
    assert params is not None and params["notebook_id"] == notebook


def test_a_fixed_fee_is_its_own_row_once_per_action(prices: Any) -> None:
    rules = PricingRules.parse({"usd_per_eur": 1, "actions": {"output:audio": {"fixed_cents": 5}}})
    action_id = uuid4()
    conn = FakeConnection()
    tts = Usage("tts", "llm", 100, 0)
    for _ in range(2):  # two segments of one audio output
        record_usage(
            conn, uuid4(), None, "rag", "output:audio", action_id, tts, prices=prices, rules=rules
        )
    assert [sql for sql, _ in conn.executed] == [USAGE_SQL, FEE_SQL, USAGE_SQL, FEE_SQL]
    fee = conn.executed[1][1]
    assert fee is not None
    assert (fee["kind"], fee["model"], fee["tokens_in"], fee["cost_cents"]) == ("fee", "fee", 0, 0)
    assert fee["charge_cents"] == 5 and fee["action_id"] == action_id
    assert "on conflict (action_id) where kind = 'fee' do nothing" in FEE_SQL


def test_the_dollar_cost_is_charged_in_euros_at_the_rate(prices: Any) -> None:
    conn = FakeConnection()
    rules = PricingRules.parse({"usd_per_eur": 1.25, "default_margin_pct": 10})
    row = record_usage(
        conn, uuid4(), None, "rag", "chat_answer", uuid4(), LLM, prices=prices, rules=rules
    )
    assert row.cost_usd_cents == Decimal("0.33")  # MODEL_PRICES are US cents
    assert row.usd_per_eur == Decimal("1.25")
    assert row.cost_cents == Decimal("0.264")  # 0.33 / 1.25, euro cents
    assert row.charge_cents == Decimal("0.2904")  # plus 10 %
    params = conn.executed[0][1]
    assert params is not None and params["cost_usd_cents"] == Decimal("0.33")
    ecb = PricingRules.parse({"usd_per_eur": 1.1188})
    assert ecb.cost_eur_cents(Decimal(100)) == Decimal("89.3814801573")


def test_without_a_rate_nothing_is_booked(prices: Any) -> None:
    conn = FakeConnection()
    with pytest.raises(ExchangeRateMissingError, match="usd_per_eur"):
        record_usage(
            conn,
            uuid4(),
            None,
            "rag",
            "chat_answer",
            uuid4(),
            LLM,
            prices=prices,
            rules=PricingRules(),
        )
    assert conn.executed == []


def test_record_usage_refuses_before_touching_the_database(prices: Any) -> None:
    conn = FakeConnection()
    with pytest.raises(UnpricedModelError):
        record_usage(
            conn,
            uuid4(),
            None,
            "ingest",
            "source:pdf",
            uuid4(),
            Usage("embedding", "nope", 1, 0),
            prices=prices,
            rules=FREE,
        )
    for action in ("unknown", "output:podcast", ""):
        with pytest.raises(ValueError, match="unknown action"):
            record_usage(
                conn, uuid4(), None, "rag", action, uuid4(), LLM, prices=prices, rules=FREE
            )
    assert conn.executed == []


async def test_async_variants(prices: Any) -> None:
    user = uuid4()
    conn = FakeAsyncConnection(rows=[(7,)])
    assert await abalance_cents(conn, user) == 7
    rules = PricingRules.parse({"usd_per_eur": 1, "actions": {"source:pdf": {"fixed_cents": 1}}})
    usage = await arecord_usage(
        conn,
        user,
        None,
        "ingest",
        "source:pdf",
        uuid4(),
        Usage("embedding", "embed", 500_000, 0),
        prices=prices,
        rules=rules,
    )
    assert usage.cost_cents == usage.charge_cents == 1
    assert [sql for sql, _ in conn.executed] == [BALANCE_SQL, USAGE_SQL, FEE_SQL]


def test_balance_reads_dict_rows_too() -> None:
    assert balance_cents(FakeConnection(rows=[{"balance_cents": 42}]), uuid4()) == 42
    assert balance_cents(FakeConnection(rows=[{"balance_cents": None}]), uuid4()) == 0


def test_record_usage_past_the_integer_columns_fails_before_the_insert(prices: Any) -> None:
    conn = FakeConnection()
    with pytest.raises(ValidationError, match="less than or equal to 2147483647"):
        record_usage(
            conn,
            uuid4(),
            None,
            "rag",
            "chat_answer",
            uuid4(),
            Usage("llm", "llm", 2**31, 0),
            prices=prices,
            rules=FREE,
        )
    assert conn.executed == []
