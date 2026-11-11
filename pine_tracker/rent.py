"""Rent period arithmetic, kept free of Flask and the database.

The awkward cases this exists to get right:

* **Split payments.** One month can be paid in several pieces: half on the
  1st, half on the 12th. Each piece is its own RentPayment row, and the
  period's status comes from their sum, not from any single row.

* **Section 8.** Part of the rent arrives from the housing authority and part
  from the tenant. A tenant who pays their whole share is square even though
  the money received is far short of the full rent, so the tenant's balance
  is tracked against the tenant's portion, not the total.

* **Late.** A balance on the 2nd is normal; a balance on the 10th is not.
  Lateness is "still owing after the grace period", which is a different
  question from "is there a balance".
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from typing import Iterable, Optional

# Rent is due on the 1st; most California leases allow a few days before a
# late fee applies. Override with RENT_GRACE_DAYS if Wei's leases differ.
DEFAULT_GRACE_DAYS = 5

PAID = "paid"
PARTIAL = "partial"
LATE = "late"
DUE = "due"
OVERPAID = "overpaid"

STATUS_LABELS = {
    PAID: "Paid",
    PARTIAL: "Part paid",
    LATE: "Late",
    DUE: "Due",
    OVERPAID: "Overpaid",
}


def period_start(day: dt.date) -> dt.date:
    """The first of the month containing ``day``. Periods are months."""
    return day.replace(day=1)


def shift_months(start: dt.date, months: int) -> dt.date:
    """Move a period start forward or back, staying on the 1st."""
    index = start.year * 12 + (start.month - 1) + months
    return dt.date(index // 12, index % 12 + 1, 1)


def period_label(start: dt.date) -> str:
    return f"{calendar.month_name[start.month]} {start.year}"


@dataclass(frozen=True)
class PeriodStatus:
    """What one lease owes and has paid for one month."""

    period: dt.date
    expected_cents: int          # full rent, tenant portion plus any subsidy
    tenant_expected_cents: int   # what the tenant themselves owes
    subsidy_expected_cents: int
    received_cents: int
    tenant_received_cents: int
    subsidy_received_cents: int
    last_payment_on: Optional[dt.date]
    payment_count: int
    status: str

    @property
    def balance_cents(self) -> int:
        return self.expected_cents - self.received_cents

    @property
    def tenant_balance_cents(self) -> int:
        return self.tenant_expected_cents - self.tenant_received_cents

    @property
    def is_split(self) -> bool:
        return self.payment_count > 1

    @property
    def is_settled(self) -> bool:
        return self.status in (PAID, OVERPAID)


def status_for(
    period: dt.date,
    rent_cents: int,
    subsidy_cents: int,
    payments: Iterable,
    today: Optional[dt.date] = None,
    grace_days: int = DEFAULT_GRACE_DAYS,
) -> PeriodStatus:
    """Summarize one lease-month.

    ``payments`` is any iterable of objects with ``received_cents``,
    ``received_on`` and ``source``; the ORM rows satisfy that, and so do
    simple stand-ins in tests.
    """
    today = today or dt.date.today()
    rows = list(payments)

    received = sum(p.received_cents for p in rows)
    subsidy_received = sum(
        p.received_cents for p in rows if p.source == "housing_authority"
    )
    tenant_received = received - subsidy_received
    dates = [p.received_on for p in rows if p.received_on]

    tenant_expected = max(rent_cents - subsidy_cents, 0)

    if received > rent_cents:
        status = OVERPAID
    elif received >= rent_cents and rent_cents > 0:
        status = PAID
    else:
        # A period that hasn't started yet is never late.
        past_grace = today >= period + dt.timedelta(days=grace_days)
        if received == 0:
            status = LATE if past_grace else DUE
        else:
            status = LATE if past_grace else PARTIAL

    return PeriodStatus(
        period=period,
        expected_cents=rent_cents,
        tenant_expected_cents=tenant_expected,
        subsidy_expected_cents=subsidy_cents,
        received_cents=received,
        tenant_received_cents=tenant_received,
        subsidy_received_cents=subsidy_received,
        last_payment_on=max(dates) if dates else None,
        payment_count=len(rows),
        status=status,
    )


def days_late(period: dt.date, today: Optional[dt.date] = None) -> int:
    """How many days past the 1st we are. Negative before the month starts."""
    today = today or dt.date.today()
    return (today - period).days
