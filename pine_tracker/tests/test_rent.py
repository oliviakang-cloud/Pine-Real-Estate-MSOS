"""Rent tracking tests.

The cases that matter are the messy ones Wei actually sees: a tenant who pays
half on the 1st and half on the 12th, a Section 8 unit where most of the money
comes from the housing authority, and a month that quietly goes past due.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

import pytest
from sqlalchemy import select

from pine_tracker import rent as rentlib
from pine_tracker.db import create_all, make_session_factory
from pine_tracker.models import Lease, Property, RentPayment, Tenant, Unit
from pine_tracker.web import create_app


@dataclass
class FakePayment:
    received_cents: int
    received_on: Optional[dt.date] = None
    source: str = "tenant"


SEPT = dt.date(2026, 9, 1)


# -- the arithmetic, with no database in the way --------------------------


def test_a_single_full_payment_is_paid():
    status = rentlib.status_for(
        SEPT, 240000, 0, [FakePayment(240000, dt.date(2026, 9, 1))],
        today=dt.date(2026, 9, 3),
    )
    assert status.status == rentlib.PAID
    assert status.balance_cents == 0
    assert not status.is_split


def test_split_payments_add_up_to_paid():
    """Half on the 1st, half on the 12th is still a paid month."""
    status = rentlib.status_for(
        SEPT, 240000, 0,
        [
            FakePayment(120000, dt.date(2026, 9, 1)),
            FakePayment(120000, dt.date(2026, 9, 12)),
        ],
        today=dt.date(2026, 9, 20),
    )
    assert status.status == rentlib.PAID
    assert status.is_split
    assert status.payment_count == 2
    assert status.last_payment_on == dt.date(2026, 9, 12)


def test_part_paid_inside_the_grace_period_is_not_late_yet():
    status = rentlib.status_for(
        SEPT, 240000, 0, [FakePayment(100000, dt.date(2026, 9, 2))],
        today=dt.date(2026, 9, 3),
    )
    assert status.status == rentlib.PARTIAL
    assert status.balance_cents == 140000


def test_the_same_shortfall_after_grace_is_late():
    status = rentlib.status_for(
        SEPT, 240000, 0, [FakePayment(100000, dt.date(2026, 9, 2))],
        today=dt.date(2026, 9, 14),
    )
    assert status.status == rentlib.LATE


def test_nothing_received_is_due_then_late():
    early = rentlib.status_for(SEPT, 240000, 0, [], today=dt.date(2026, 9, 2))
    assert early.status == rentlib.DUE

    later = rentlib.status_for(SEPT, 240000, 0, [], today=dt.date(2026, 9, 11))
    assert later.status == rentlib.LATE


def test_a_future_month_is_never_late():
    status = rentlib.status_for(
        dt.date(2026, 12, 1), 240000, 0, [], today=dt.date(2026, 9, 15)
    )
    assert status.status == rentlib.DUE


def test_section_8_tenant_portion_tracked_separately():
    """The tenant owes $350; the authority owes $2,050. Both arrive apart."""
    status = rentlib.status_for(
        SEPT, 240000, 205000,
        [
            FakePayment(205000, dt.date(2026, 9, 3), source="housing_authority"),
            FakePayment(35000, dt.date(2026, 9, 5), source="tenant"),
        ],
        today=dt.date(2026, 9, 30),
    )
    assert status.status == rentlib.PAID
    assert status.tenant_expected_cents == 35000
    assert status.tenant_received_cents == 35000
    assert status.subsidy_received_cents == 205000
    assert status.tenant_balance_cents == 0


def test_section_8_tenant_short_even_though_most_money_arrived():
    status = rentlib.status_for(
        SEPT, 240000, 205000,
        [FakePayment(205000, dt.date(2026, 9, 3), source="housing_authority")],
        today=dt.date(2026, 9, 20),
    )
    assert status.status == rentlib.LATE
    assert status.tenant_balance_cents == 35000


def test_overpayment_is_flagged_rather_than_hidden():
    status = rentlib.status_for(
        SEPT, 240000, 0, [FakePayment(250000, dt.date(2026, 9, 1))],
        today=dt.date(2026, 9, 2),
    )
    assert status.status == rentlib.OVERPAID
    assert status.balance_cents == -10000


def test_month_arithmetic_crosses_year_boundaries():
    assert rentlib.shift_months(dt.date(2026, 1, 1), -1) == dt.date(2025, 12, 1)
    assert rentlib.shift_months(dt.date(2026, 12, 1), 1) == dt.date(2027, 1, 1)
    assert rentlib.period_start(dt.date(2026, 9, 27)) == SEPT
    assert rentlib.period_label(SEPT) == "September 2026"


# -- the screens ----------------------------------------------------------


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    application = create_app(f"sqlite:///{tmp_path/'rent.db'}")
    create_all(application.config["ENGINE"])
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def lease_id(app, client):
    client.get("/")  # creates the organization
    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        prop = Property(organization_id=1, street="1650 Frisbie Ct")
        db.add(prop)
        db.flush()
        unit = Unit(organization_id=1, property_id=prop.id, label="Unit 2")
        db.add(unit)
        db.flush()
        tenant = Tenant(organization_id=1, name="J. Rivera")
        db.add(tenant)
        db.flush()
        lease = Lease(
            organization_id=1, unit_id=unit.id, tenant_id=tenant.id,
            starts_on=dt.date(2026, 1, 1), rent_cents=240000, active=True,
        )
        db.add(lease)
        db.commit()
        return lease.id


def test_adding_a_tenant_creates_the_lease(client, app):
    client.get("/")
    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        prop = Property(organization_id=1, street="125 Sandpiper Dr")
        db.add(prop)
        db.flush()
        unit = Unit(organization_id=1, property_id=prop.id, label="Main")
        db.add(unit)
        db.commit()
        unit_id = unit.id

    client.post(
        "/leases/new",
        data={
            "unit_id": str(unit_id), "tenant_name": "A. Chen",
            "rent": "2400", "subsidy": "2050", "deposit": "2400",
            "phone": "", "starts_on": "2026-01-01",
        },
        follow_redirects=True,
    )
    with Session() as db:
        lease = db.scalar(select(Lease))
        tenant = db.scalar(select(Tenant))
    assert tenant.name == "A. Chen"
    assert lease.rent_cents == 240000
    assert lease.subsidy_cents == 205000


def test_recording_two_payments_for_one_month(client, app, lease_id):
    for amount, day in [("1200", "2026-09-01"), ("1200", "2026-09-12")]:
        client.post(
            f"/rent/{lease_id}/payment",
            data={"amount": amount, "period": "2026-09",
                  "received_on": day, "source": "tenant", "method": "Zelle"},
        )

    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        payments = db.scalars(select(RentPayment)).all()
    assert len(payments) == 2
    assert sum(p.received_cents for p in payments) == 240000

    page = client.get(f"/rent/{lease_id}").get_data(as_text=True)
    assert "September 2026" in page
    assert "Paid" in page


def test_rent_overview_shows_the_shortfall(client, lease_id):
    client.post(
        f"/rent/{lease_id}/payment",
        data={"amount": "1000", "period": "2026-09",
              "received_on": "2026-09-02", "source": "tenant", "method": ""},
    )
    page = client.get("/rent?month=2026-09").get_data(as_text=True)
    assert "J. Rivera" in page
    assert "$1,400.00 short" in page


def test_a_payment_can_be_removed(client, app, lease_id):
    client.post(
        f"/rent/{lease_id}/payment",
        data={"amount": "2400", "period": "2026-09",
              "received_on": "2026-09-01", "source": "tenant", "method": ""},
    )
    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        payment_id = db.scalar(select(RentPayment)).id

    client.post(f"/rent/payment/{payment_id}/delete", follow_redirects=True)
    with Session() as db:
        assert db.scalars(select(RentPayment)).all() == []


def test_bad_amount_is_refused_without_saving(client, app, lease_id):
    page = client.post(
        f"/rent/{lease_id}/payment",
        data={"amount": "about two thousand", "period": "2026-09",
              "received_on": "2026-09-01", "source": "tenant", "method": ""},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "Enter the amount received" in page

    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        assert db.scalars(select(RentPayment)).all() == []


def test_rent_is_a_tab_and_add_expense_is_not(client, lease_id):
    page = client.get("/rent").get_data(as_text=True)
    assert ">Rent<" in page
    assert 'aria-current="page"' in page
    # Adding an expense now starts from a property, so it isn't a tab.
    assert ">Add expense<" not in page


def test_expense_form_still_reachable_and_keeps_properties_lit(client, lease_id):
    page = client.get("/expense/new").get_data(as_text=True)
    assert page.count('aria-current="page"') == 1
    assert "Add an expense" in page
