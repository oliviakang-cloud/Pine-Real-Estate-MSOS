"""Money helpers.

Rule for this project: money is stored as integer cents, never as float.

The existing spreadsheet shows why. Cells such as ``=1.17*1.1025+6.28*1.0875``
evaluate to 8.1194249999999997, and a year of those values never sums to the
number on the bank statement. Every amount that enters the database is rounded
to cents exactly once, at the boundary, using ROUND_HALF_UP.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Sequence

__all__ = ["to_cents", "from_cents", "fmt", "allocate_cents"]


def to_cents(value: Decimal | float | int | str) -> int:
    """Round a monetary value to whole cents.

    Floats are converted through ``str`` so that 8.1194249999999997 is treated
    as the decimal literal a human would have typed, not as its binary
    expansion.
    """
    if isinstance(value, int):
        return value * 100
    if isinstance(value, float):
        value = str(value)
    dec = Decimal(value)
    return int((dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_cents(cents: int) -> Decimal:
    """Cents back to a two-place Decimal, for display and export."""
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


def fmt(cents: int) -> str:
    """Format cents as ``$1,052.38`` (negatives as ``-$12.00``)."""
    sign = "-" if cents < 0 else ""
    return f"{sign}${from_cents(abs(cents)):,.2f}"


def allocate_cents(total_cents: int, weights: Sequence[int]) -> list[int]:
    """Split ``total_cents`` across ``weights`` so the parts sum exactly.

    Used to spread receipt-level tax, discount, and delivery charges across
    line items. Largest-remainder method: everyone gets the floor share, then
    the leftover pennies go to the lines with the largest fractional parts.

    On the sample Home Depot order this is what pushes $90.31 of sales tax and
    $39.00 of delivery down onto the individual lines without losing a cent.
    """
    if not weights:
        return []
    weight_total = sum(weights)
    if weight_total == 0:
        # Degenerate case: spread evenly rather than dividing by zero.
        base, leftover = divmod(total_cents, len(weights))
        return [base + (1 if i < leftover else 0) for i in range(len(weights))]

    shares: list[int] = []
    remainders: list[tuple[int, int]] = []  # (remainder, index)
    for i, w in enumerate(weights):
        numerator = total_cents * w
        base, rem = divmod(numerator, weight_total)
        shares.append(base)
        remainders.append((rem, i))

    leftover = total_cents - sum(shares)
    # Largest remainder first; ties broken by original order for determinism.
    remainders.sort(key=lambda pair: (-pair[0], pair[1]))
    step = 1 if leftover >= 0 else -1
    for _ in range(abs(leftover)):
        _, idx = remainders.pop(0)
        shares[idx] += step
        remainders.append((0, idx))
    return shares


def sum_cents(values: Iterable[int]) -> int:
    return sum(values)
