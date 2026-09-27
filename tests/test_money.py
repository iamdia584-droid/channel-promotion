"""Money arithmetic must be exact and must refuse floats (spec §11)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import Float

from app.core.money import (
    D,
    MoneyError,
    cpm_cost,
    impressions_for_budget,
    pct,
    q,
    split_commission,
)
from app.models import Base


def test_float_is_rejected_not_rounded():
    # 0.1 + 0.2 != 0.3 in binary floating point. Accepting a float would import
    # that error into the ledger, so D() refuses outright.
    with pytest.raises(MoneyError, match="float is not permitted"):
        D(0.1)
    with pytest.raises(MoneyError):
        cpm_cost(1000, 50.5)


def test_bool_is_rejected():
    with pytest.raises(MoneyError):
        D(True)


def test_spec_section_6_example():
    """Budget ৳10,000 at ৳50 CPM buys 200,000 impressions."""
    assert impressions_for_budget("10000", "50") == 200_000


def test_spec_section_11_example():
    """50,000 impressions: publisher ৳4,000 at ৳80 CPM, platform ৳1,000 at 20%."""
    gross = cpm_cost(50_000, "100")
    assert gross == Decimal("5000.000000")
    publisher, platform = split_commission(gross, "0.20")
    assert publisher == Decimal("4000.000000")
    assert platform == Decimal("1000.000000")
    assert cpm_cost(50_000, "80") == publisher


def test_commission_split_never_creates_or_destroys_money():
    # Deliberately awkward amounts and rates: the two parts must still reconstruct
    # the gross exactly, at every rate.
    for gross in ["0.000001", "0.000003", "1", "33.333333", "999999.999999", "7.77"]:
        for rate in ["0", "0.0001", "0.1", "0.3333", "0.5", "0.6667", "1"]:
            publisher, platform = split_commission(gross, rate)
            assert publisher + platform == q(gross), (gross, rate)
            assert publisher >= 0 and platform >= 0


def test_commission_rate_must_be_a_fraction():
    with pytest.raises(MoneyError):
        split_commission("100", "1.5")
    with pytest.raises(MoneyError):
        split_commission("100", "-0.1")


def test_cpm_cost_is_exact_for_single_impressions():
    # One impression at ৳50 CPM is ৳0.05 exactly — this is why storage scale is 6,
    # not 2. Accruing it 1000 times must land exactly on ৳50.
    unit = cpm_cost(1, "50")
    assert unit == Decimal("0.050000")
    assert sum([unit] * 1000) == Decimal("50.000000")


def test_cpm_cost_rejects_negatives():
    with pytest.raises(MoneyError):
        cpm_cost(-1, "50")
    with pytest.raises(MoneyError):
        cpm_cost(100, "-50")


def test_impressions_for_budget_rounds_down():
    # 999/50*1000 = 19980 exactly; 1000.5 must not round up into unfunded inventory.
    assert impressions_for_budget("999", "50") == 19_980
    assert impressions_for_budget("50.049", "50") == 1000


def test_pct_handles_zero_denominator():
    assert pct(5, 0) == Decimal(0)
    assert pct(25, 1000) == Decimal("2.5000")


def test_no_float_columns_in_schema():
    """A Float column anywhere in the schema would silently corrupt balances."""
    offenders = [
        f"{table.name}.{col.name}"
        for table in Base.metadata.tables.values()
        for col in table.columns
        if isinstance(col.type, Float)
    ]
    assert offenders == []
