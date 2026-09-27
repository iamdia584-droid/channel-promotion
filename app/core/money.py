"""Fixed-point money. Floats are forbidden in financial code (spec §11).

Every monetary value in this system is a ``decimal.Decimal`` quantized to six
decimal places. Six places (rather than two) because CPM arithmetic divides by
1000 and we must not lose sub-paisa precision when accruing a single impression;
rounding to presentation precision happens only at the payout boundary.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

SCALE = 6
QUANT = Decimal(1).scaleb(-SCALE)  # 0.000001
ZERO = Decimal("0.000000")
IMPRESSION_UNIT = Decimal(1000)  # CPM = cost per 1000 impressions


class MoneyError(ValueError):
    """Raised on malformed or mixed-currency money operations."""


def D(value: object) -> Decimal:
    """Coerce to Decimal, rejecting floats outright.

    A float argument is a bug, not something to round: accepting it would
    silently import binary rounding error into the ledger.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise MoneyError(f"bool is not a monetary value: {value!r}")
    if isinstance(value, float):
        raise MoneyError(
            f"float is not permitted in financial arithmetic: {value!r}. "
            "Pass a str or Decimal."
        )
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation as exc:
            raise MoneyError(f"not a decimal: {value!r}") from exc
    raise MoneyError(f"unsupported money type {type(value).__name__}")


def q(value: object) -> Decimal:
    """Quantize to storage scale, half-up (the conventional accounting rounding)."""
    with localcontext() as ctx:
        ctx.prec = 40
        return D(value).quantize(QUANT, rounding=ROUND_HALF_UP)


def q_down(value: object) -> Decimal:
    """Quantize toward zero. Used where rounding up would create money."""
    with localcontext() as ctx:
        ctx.prec = 40
        return D(value).quantize(QUANT, rounding=ROUND_DOWN)


def cpm_cost(impressions: int, cpm: object) -> Decimal:
    """Cost of ``impressions`` at ``cpm``. The only /1000 in the codebase.

    >>> cpm_cost(50_000, "80")
    Decimal('4000.000000')
    """
    if impressions < 0:
        raise MoneyError("impressions cannot be negative")
    rate = D(cpm)
    if rate < 0:
        raise MoneyError("cpm cannot be negative")
    with localcontext() as ctx:
        ctx.prec = 40
        return q(Decimal(impressions) * rate / IMPRESSION_UNIT)


def impressions_for_budget(budget: object, cpm: object) -> int:
    """How many whole impressions ``budget`` buys at ``cpm``. Rounds down."""
    rate = D(cpm)
    if rate <= 0:
        raise MoneyError("cpm must be positive to derive impressions")
    with localcontext() as ctx:
        ctx.prec = 40
        return int((D(budget) * IMPRESSION_UNIT / rate).to_integral_value(rounding=ROUND_DOWN))


def split_commission(gross: object, commission_rate: object) -> tuple[Decimal, Decimal]:
    """Split ``gross`` into (publisher_share, platform_share).

    The platform share is derived by subtraction so the two parts always sum to
    exactly ``gross`` — no rounding dust can be created or destroyed.
    """
    g = q(gross)
    rate = D(commission_rate)
    if not (Decimal(0) <= rate <= Decimal(1)):
        raise MoneyError(f"commission rate must be in [0, 1], got {rate}")
    platform = q(g * rate)
    publisher = g - platform
    assert publisher + platform == g  # invariant, not a guess
    return publisher, platform


def pct(part: object, whole: object, places: int = 4) -> Decimal:
    """Percentage helper for CTR etc. Returns 0 rather than dividing by zero."""
    w = D(whole)
    if w == 0:
        return Decimal(0)
    with localcontext() as ctx:
        ctx.prec = 40
        return (D(part) * 100 / w).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def fmt(value: object, currency: str = "BDT", places: int = 2) -> str:
    """Human presentation. Never feed this back into arithmetic."""
    symbol = {"BDT": "৳", "USD": "$", "EUR": "€"}.get(currency.upper(), currency.upper() + " ")
    amount = D(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return f"{symbol}{amount:,}"
