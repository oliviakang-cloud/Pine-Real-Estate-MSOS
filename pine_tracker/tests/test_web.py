"""Web tests. Each one drives a real request against a real database.

The point is to catch the failures that matter for a field tool: a screen
that 500s, an expense that silently doesn't save, or a page that leaks
another organization's rows.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import select

from pine_tracker.db import create_all
from pine_tracker.import_expenses import import_workbook
from pine_tracker.models import ExpenseLine, Property
from pine_tracker.web import create_app

SAMPLE = (
    Path(__file__).resolve().parents[1]
    / "sample_data"
    / "Sample_Operating_Expenses_Tracking_for_September_2026.xlsx"
)


@pytest.fixture()
def app(tmp_path):
    application = create_app(f"sqlite:///{tmp_path/'test.db'}")
    create_all(application.config["ENGINE"])
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def seeded(app, client):
    """An app whose database holds the real September workbook."""
    from pine_tracker.db import make_session_factory

    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        import_workbook(db, SAMPLE)
        db.commit()
    return client


def test_empty_property_list_invites_action(client):
    page = client.get("/").get_data(as_text=True)
    assert "No properties yet" in page


def test_property_list_shows_imported_totals(seeded):
    page = seeded.get("/").get_data(as_text=True)
    assert "1650 Frisbie Ct" in page
    assert "$2,161.73" in page  # the September sheet total


def test_property_page_lists_its_own_expenses(seeded, app):
    from pine_tracker.db import make_session_factory

    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        prop = db.scalar(select(Property).where(Property.street.like("%Frisbie%")))

    page = seeded.get(f"/property/{prop.id}").get_data(as_text=True)
    assert "Unit 5" in page or "Unit 1" in page
    assert "Where the money went" in page


def test_missing_property_is_404(client):
    assert client.get("/property/9999").status_code == 404


def test_saving_an_expense_persists_it(client, app):
    from pine_tracker.db import make_session_factory

    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        prop = Property(organization_id=1, street="123 Test Ave", short_name="Test Ave")
        db.add(prop)
        db.commit()
        property_id = prop.id

    client.get("/")  # creates the organization row
    response = client.post(
        "/expense/new",
        data={
            "property_id": str(property_id),
            "amount": "48.62",
            "description": "Toilet flapper and wax ring",
            "incurred_on": dt.date(2026, 9, 27).isoformat(),
            "unit_id": "",
            "category": "",
            "treatment": "",
            "vendor_id": "",
            "paid_by_member_id": "",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    with Session() as db:
        line = db.scalar(select(ExpenseLine).where(ExpenseLine.property_id == property_id))
    assert line is not None
    assert line.amount_cents == 4862
    assert line.source == "manual"


def test_bad_amount_is_rejected_with_a_readable_message(client, app):
    from pine_tracker.db import make_session_factory

    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        prop = Property(organization_id=1, street="9 Oak St", short_name="Oak St")
        db.add(prop)
        db.commit()
        property_id = prop.id

    client.get("/")
    page = client.post(
        "/expense/new",
        data={
            "property_id": str(property_id),
            "amount": "forty dollars",
            "description": "Paint",
            "incurred_on": "2026-09-27",
            "unit_id": "", "category": "", "treatment": "",
            "vendor_id": "", "paid_by_member_id": "",
        },
    ).get_data(as_text=True)
    assert "Amount should be a number" in page

    with Session() as db:
        assert db.scalars(select(ExpenseLine)).all() == []


def test_review_queue_holds_the_flagged_lines(seeded):
    page = seeded.get("/review").get_data(as_text=True)
    # The $1,500 tub surround labor is the line the importer refuses to guess.
    assert "$1,500.00" in page or "tub surround" in page.lower()


def test_password_gate_blocks_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    application = create_app(f"sqlite:///{tmp_path/'gated.db'}")
    create_all(application.config["ENGINE"])
    gated = application.test_client()

    assert gated.get("/").status_code == 302

    gated.post("/sign-in", data={"password": "hunter2"})
    assert gated.get("/").status_code == 200


def test_dates_render_without_platform_specific_formats(app):
    """Guard against strftime extensions like %-d that fail on Windows.

    The first version of these templates used "%b %-d", which works on Linux
    and raises ValueError on Windows, so every page with a date 500'd there.
    """
    import datetime as date_mod

    with app.app_context():
        short = app.jinja_env.filters["short_date"]
        long_ = app.jinja_env.filters["long_date"]
        day = date_mod.date(2026, 9, 5)
        assert short(day) == "Sep 5"
        assert long_(day) == "Sep 5, 2026"

    source_dir = Path(__file__).resolve().parents[1] / "templates"
    for template in source_dir.glob("*.html"):
        assert "strftime" not in template.read_text(), (
            f"{template.name} calls strftime directly; use the short_date or "
            "long_date filter so Windows and Linux agree."
        )
