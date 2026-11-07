"""The web app: property list, property ledger, and mobile expense entry.

Kept deliberately small. Everything the app knows about money and categories
lives in models.py, money.py and classify.py; this module is routing and HTML.
"""

from __future__ import annotations

import datetime as dt
import os
from functools import wraps
from pathlib import Path
from typing import Optional

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session as flask_session,
    url_for,
)
from sqlalchemy import func, select

from .classify import classify
from .db import create_all, make_engine, make_session_factory
from .models import (
    CostTreatment,
    ExpenseLine,
    Member,
    Organization,
    Property,
    ScheduleE,
    Unit,
    Vendor,
)
from .money import fmt, to_cents

CATEGORY_LABELS = {
    ScheduleE.ADVERTISING: "Advertising",
    ScheduleE.AUTO_TRAVEL: "Auto and travel",
    ScheduleE.CLEANING_MAINTENANCE: "Cleaning and maintenance",
    ScheduleE.COMMISSIONS: "Commissions",
    ScheduleE.INSURANCE: "Insurance",
    ScheduleE.LEGAL_PROFESSIONAL: "Legal and professional",
    ScheduleE.MANAGEMENT_FEES: "Management fees",
    ScheduleE.MORTGAGE_INTEREST: "Mortgage interest",
    ScheduleE.OTHER_INTEREST: "Other interest",
    ScheduleE.REPAIRS: "Repairs",
    ScheduleE.SUPPLIES: "Supplies",
    ScheduleE.TAXES: "Taxes",
    ScheduleE.UTILITIES: "Utilities",
    ScheduleE.OTHER: "Other",
    ScheduleE.UNCATEGORIZED: "Not categorized",
}

TREATMENT_LABELS = {
    CostTreatment.REPAIR: "Repair (deduct this year)",
    CostTreatment.CAPITAL: "Improvement (depreciate)",
    CostTreatment.UNDECIDED: "Decide later",
}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if _password() and not flask_session.get("signed_in"):
            return redirect(url_for("sign_in", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def _password() -> Optional[str]:
    return os.environ.get("APP_PASSWORD") or None


def create_app(database_url: Optional[str] = None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-not-a-secret")
    app.config["ORG_SLUG"] = os.environ.get("ORG_SLUG", "pine")
    app.config["ORG_NAME"] = os.environ.get("ORG_NAME", "Pine Real Estate Management")

    engine = make_engine(database_url)
    app.config["ENGINE"] = engine
    Session = make_session_factory(engine)

    if not _password():
        app.logger.warning(
            "APP_PASSWORD is not set: anyone with the URL can read and edit. "
            "Set it before sharing a deployed link."
        )

    @app.before_request
    def open_session():
        g.db = Session()

    @app.teardown_request
    def close_session(exc):
        db = g.pop("db", None)
        if db is None:
            return
        if exc:
            db.rollback()
        db.close()

    def current_org() -> Organization:
        org = g.db.scalar(
            select(Organization).where(Organization.slug == app.config["ORG_SLUG"])
        )
        if org is None:
            org = Organization(
                slug=app.config["ORG_SLUG"], name=app.config["ORG_NAME"]
            )
            g.db.add(org)
            g.db.commit()
        return org

    # -- sign in -----------------------------------------------------------

    @app.route("/sign-in", methods=["GET", "POST"])
    def sign_in():
        if not _password():
            return redirect(url_for("properties"))
        if request.method == "POST":
            if request.form.get("password") == _password():
                flask_session["signed_in"] = True
                flask_session.permanent = True
                return redirect(request.args.get("next") or url_for("properties"))
            flash("That password didn't match. Try again.", "error")
        return render_template("sign_in.html")

    @app.post("/sign-out")
    def sign_out():
        flask_session.clear()
        return redirect(url_for("sign_in"))

    # -- property list -----------------------------------------------------

    @app.get("/")
    @login_required
    def properties():
        org = current_org()
        year = int(request.args.get("year", dt.date.today().year))
        jan1 = dt.date(year, 1, 1)
        dec31 = dt.date(year, 12, 31)

        totals = dict(
            g.db.execute(
                select(ExpenseLine.property_id, func.sum(ExpenseLine.amount_cents))
                .where(
                    ExpenseLine.organization_id == org.id,
                    ExpenseLine.incurred_on >= jan1,
                    ExpenseLine.incurred_on <= dec31,
                )
                .group_by(ExpenseLine.property_id)
            ).all()
        )

        props = g.db.scalars(
            select(Property)
            .where(Property.organization_id == org.id, Property.active.is_(True))
            .order_by(Property.street)
        ).all()

        review_count = g.db.scalar(
            select(func.count())
            .select_from(ExpenseLine)
            .where(
                ExpenseLine.organization_id == org.id,
                ExpenseLine.needs_review.is_(True),
            )
        )

        rows = [(p, totals.get(p.id, 0)) for p in props]
        rows.sort(key=lambda r: (-r[1], r[0].street))
        return render_template(
            "properties.html",
            rows=rows,
            year=year,
            year_total=sum(totals.values()),
            review_count=review_count or 0,
        )

    # -- one property ------------------------------------------------------

    @app.get("/property/<int:property_id>")
    @login_required
    def property_detail(property_id: int):
        org = current_org()
        prop = g.db.get(Property, property_id)
        if prop is None or prop.organization_id != org.id:
            abort(404)

        lines = g.db.scalars(
            select(ExpenseLine)
            .where(
                ExpenseLine.organization_id == org.id,
                ExpenseLine.property_id == prop.id,
            )
            .order_by(ExpenseLine.incurred_on.desc(), ExpenseLine.id.desc())
            .limit(200)
        ).all()

        units = {
            u.id: u
            for u in g.db.scalars(
                select(Unit).where(Unit.property_id == prop.id)
            ).all()
        }

        by_category: dict[str, int] = {}
        for line in lines:
            by_category[line.category] = by_category.get(line.category, 0) + line.amount_cents

        return render_template(
            "property.html",
            prop=prop,
            lines=lines,
            units=units,
            total=sum(line.amount_cents for line in lines),
            by_category=sorted(by_category.items(), key=lambda kv: -kv[1]),
            category_labels=CATEGORY_LABELS,
        )

    # -- add an expense ----------------------------------------------------

    @app.route("/expense/new", methods=["GET", "POST"])
    @login_required
    def expense_new():
        org = current_org()
        props = g.db.scalars(
            select(Property)
            .where(Property.organization_id == org.id, Property.active.is_(True))
            .order_by(Property.street)
        ).all()
        units = g.db.scalars(
            select(Unit).where(Unit.organization_id == org.id).order_by(Unit.label)
        ).all()
        members = g.db.scalars(
            select(Member).where(Member.organization_id == org.id).order_by(Member.name)
        ).all()
        vendors = g.db.scalars(
            select(Vendor).where(Vendor.organization_id == org.id).order_by(Vendor.name)
        ).all()

        form = {
            "property_id": request.args.get("property_id", ""),
            "incurred_on": dt.date.today().isoformat(),
            "description": "",
            "amount": "",
            "unit_id": "",
            "category": "",
            "treatment": "",
            "vendor_id": "",
            "paid_by_member_id": "",
        }

        if request.method == "POST":
            form.update({k: request.form.get(k, "").strip() for k in form})
            errors = []

            if not form["property_id"]:
                errors.append("Pick a property.")
            if not form["description"]:
                errors.append("Add a short description so this is findable later.")
            try:
                amount_cents = to_cents(form["amount"])
                if amount_cents == 0:
                    errors.append("Enter an amount.")
            except Exception:
                errors.append("Amount should be a number, like 48.62.")
                amount_cents = 0
            try:
                incurred = dt.date.fromisoformat(form["incurred_on"])
            except ValueError:
                errors.append("Date should look like 2026-09-27.")
                incurred = dt.date.today()

            if errors:
                for message in errors:
                    flash(message, "error")
            else:
                guess = classify(form["description"], amount_cents)
                line = ExpenseLine(
                    organization_id=org.id,
                    property_id=int(form["property_id"]),
                    unit_id=int(form["unit_id"]) if form["unit_id"] else None,
                    vendor_id=int(form["vendor_id"]) if form["vendor_id"] else None,
                    paid_by_member_id=(
                        int(form["paid_by_member_id"])
                        if form["paid_by_member_id"]
                        else None
                    ),
                    incurred_on=incurred,
                    description=form["description"],
                    amount_cents=amount_cents,
                    pretax_cents=amount_cents,
                    category=form["category"] or guess.category,
                    treatment=form["treatment"] or guess.treatment,
                    source="manual",
                    needs_review=not form["category"] and guess.needs_review,
                    review_note=None if form["category"] else guess.review_note,
                )
                g.db.add(line)
                g.db.commit()
                flash(f"Saved {fmt(amount_cents)}.", "ok")
                return redirect(
                    url_for("property_detail", property_id=line.property_id)
                )

        return render_template(
            "expense_form.html",
            form=form,
            props=props,
            units=units,
            members=members,
            vendors=vendors,
            categories=CATEGORY_LABELS,
            treatments=TREATMENT_LABELS,
        )

    # -- review queue ------------------------------------------------------

    @app.get("/review")
    @login_required
    def review():
        org = current_org()
        lines = g.db.scalars(
            select(ExpenseLine)
            .where(
                ExpenseLine.organization_id == org.id,
                ExpenseLine.needs_review.is_(True),
            )
            .order_by(ExpenseLine.amount_cents.desc())
        ).all()
        props = {
            p.id: p
            for p in g.db.scalars(
                select(Property).where(Property.organization_id == org.id)
            ).all()
        }
        return render_template(
            "review.html", lines=lines, props=props, category_labels=CATEGORY_LABELS
        )

    @app.post("/review/<int:line_id>")
    @login_required
    def review_resolve(line_id: int):
        org = current_org()
        line = g.db.get(ExpenseLine, line_id)
        if line is None or line.organization_id != org.id:
            abort(404)
        category = request.form.get("category")
        treatment = request.form.get("treatment")
        if category:
            line.category = category
        if treatment:
            line.treatment = treatment
        line.needs_review = False
        line.review_note = None
        g.db.commit()
        flash("Marked reviewed.", "ok")
        return redirect(url_for("review"))

    # -- template helpers --------------------------------------------------

    # strftime's "%-d" is a glibc extension and raises on Windows, so build
    # the day number ourselves. These must stay platform-independent: the app
    # is developed on Windows and deployed on Linux.
    def short_date(value: dt.date) -> str:
        return f"{value.strftime('%b')} {value.day}"

    def long_date(value: dt.date) -> str:
        return f"{value.strftime('%b')} {value.day}, {value.year}"

    app.jinja_env.filters["money"] = fmt
    app.jinja_env.filters["short_date"] = short_date
    app.jinja_env.filters["long_date"] = long_date
    app.jinja_env.globals["category_label"] = lambda key: CATEGORY_LABELS.get(key, key)

    # -- CLI ---------------------------------------------------------------

    @app.cli.command("init-db")
    def init_db_command():
        """Create any missing tables."""
        create_all(engine)
        print("Tables are up to date.")

    @app.cli.command("import-sheet")
    def import_sheet_command():
        """Load a monthly expense workbook into this database."""
        import click

        from .import_expenses import import_workbook, print_report

        path = click.prompt("Workbook path", type=click.Path(exists=True))
        db = Session()
        try:
            stats = import_workbook(
                db, Path(path), org_slug=app.config["ORG_SLUG"],
                org_name=app.config["ORG_NAME"],
            )
            db.commit()
            print_report(db, stats, app.config["ORG_SLUG"])
        finally:
            db.close()

    return app
