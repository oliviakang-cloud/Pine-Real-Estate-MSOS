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
    send_file,
    session as flask_session,
    url_for,
)
from sqlalchemy import func, select

from .classify import classify
from .db import create_all, make_engine, make_session_factory
from .models import (
    CostTreatment,
    ExpenseLine,
    Job,
    JobStatus,
    Member,
    Organization,
    Photo,
    PhotoKind,
    Property,
    Receipt,
    ScheduleE,
    Unit,
    Vendor,
)
from .money import fmt, to_cents
from .storage import UploadError, resolve, save_upload

JOB_STATUS_LABELS = {
    JobStatus.REPORTED: "Reported",
    JobStatus.PARTS_ORDERED: "Parts ordered",
    JobStatus.SCHEDULED: "Scheduled",
    JobStatus.IN_PROGRESS: "In progress",
    JobStatus.DONE: "Done",
    JobStatus.CANCELLED: "Cancelled",
}

PHOTO_KIND_LABELS = {
    PhotoKind.BEFORE_REPAIR: "Before",
    PhotoKind.AFTER_REPAIR: "After",
    PhotoKind.GENERAL: "Note",
}

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

        receipt_ids = {line.receipt_id for line in lines if line.receipt_id}
        receipts = {}
        if receipt_ids:
            receipts = {
                r.id: r.image_key
                for r in g.db.scalars(
                    select(Receipt).where(Receipt.id.in_(receipt_ids))
                ).all()
                if r.image_key
            }

        return render_template(
            "property.html",
            prop=prop,
            lines=lines,
            units=units,
            receipts=receipts,
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
                key = _store(request.files.get("receipt"), "receipts")
                if key:
                    receipt = Receipt(
                        organization_id=org.id,
                        vendor_id=line.vendor_id,
                        purchased_on=incurred,
                        total_cents=amount_cents,
                        image_key=key,
                    )
                    g.db.add(receipt)
                    g.db.flush()
                    line.receipt_id = receipt.id

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

    # -- media -------------------------------------------------------------

    @app.get("/media/<path:key>")
    @login_required
    def media(key: str):
        """Serve an uploaded file.

        Behind the password gate on purpose: these are photos of Wei's
        properties and his receipts, and the keys are random rather than
        guessable, but the gate is what actually keeps them private.
        """
        try:
            path = resolve(key)
        except UploadError:
            abort(404)
        if not path.is_file():
            abort(404)
        return send_file(path, max_age=60 * 60 * 24 * 30)

    def _store(file_storage, prefix: str) -> Optional[str]:
        """Save an optional uploaded file, flashing anything the user can fix."""
        if file_storage is None or not file_storage.filename:
            return None
        try:
            key, _ = save_upload(file_storage.stream, file_storage.filename, prefix)
            return key
        except UploadError as problem:
            flash(str(problem), "error")
            return None

    # -- repair jobs -------------------------------------------------------

    @app.get("/jobs")
    @login_required
    def jobs():
        org = current_org()
        show_done = request.args.get("show") == "done"

        query = select(Job).where(Job.organization_id == org.id)
        if show_done:
            query = query.where(Job.status.in_([JobStatus.DONE, JobStatus.CANCELLED]))
        else:
            query = query.where(Job.status.in_(list(JobStatus.OPEN_STATES)))

        items = g.db.scalars(
            query.order_by(Job.priority, Job.reported_on.desc(), Job.id.desc())
        ).all()
        props = {
            p.id: p
            for p in g.db.scalars(
                select(Property).where(Property.organization_id == org.id)
            ).all()
        }
        return render_template(
            "jobs.html",
            jobs=items,
            props=props,
            show_done=show_done,
            status_labels=JOB_STATUS_LABELS,
        )

    @app.route("/jobs/new", methods=["GET", "POST"])
    @login_required
    def job_new():
        org = current_org()
        props = g.db.scalars(
            select(Property)
            .where(Property.organization_id == org.id, Property.active.is_(True))
            .order_by(Property.street)
        ).all()
        units = g.db.scalars(
            select(Unit).where(Unit.organization_id == org.id).order_by(Unit.label)
        ).all()

        form = {
            "property_id": request.args.get("property_id", ""),
            "unit_id": "",
            "title": "",
            "description": "",
            "priority": "3",
        }

        if request.method == "POST":
            form.update({k: request.form.get(k, "").strip() for k in form})
            if not form["property_id"] or not form["title"]:
                flash("A property and a short title are needed.", "error")
            else:
                job = Job(
                    organization_id=org.id,
                    property_id=int(form["property_id"]),
                    unit_id=int(form["unit_id"]) if form["unit_id"] else None,
                    title=form["title"],
                    description=form["description"] or None,
                    status=JobStatus.REPORTED,
                    priority=int(form["priority"] or 3),
                    reported_on=dt.date.today(),
                )
                g.db.add(job)
                g.db.commit()

                key = _store(request.files.get("photo"), "jobs")
                if key:
                    g.db.add(
                        Photo(
                            organization_id=org.id,
                            property_id=job.property_id,
                            unit_id=job.unit_id,
                            job_id=job.id,
                            kind=PhotoKind.BEFORE_REPAIR,
                            caption=request.form.get("caption", "").strip() or None,
                            taken_at=dt.datetime.now(dt.timezone.utc),
                            image_key=key,
                        )
                    )
                    g.db.commit()
                return redirect(url_for("job_detail", job_id=job.id))

        return render_template("job_form.html", form=form, props=props, units=units)

    @app.get("/jobs/<int:job_id>")
    @login_required
    def job_detail(job_id: int):
        org = current_org()
        job = g.db.get(Job, job_id)
        if job is None or job.organization_id != org.id:
            abort(404)

        photos = g.db.scalars(
            select(Photo)
            .where(Photo.job_id == job.id)
            .order_by(Photo.taken_at, Photo.id)
        ).all()
        lines = g.db.scalars(
            select(ExpenseLine)
            .where(ExpenseLine.job_id == job.id)
            .order_by(ExpenseLine.incurred_on)
        ).all()

        return render_template(
            "job.html",
            job=job,
            prop=g.db.get(Property, job.property_id),
            unit=g.db.get(Unit, job.unit_id) if job.unit_id else None,
            photos=photos,
            lines=lines,
            spent=sum(line.amount_cents for line in lines),
            status_labels=JOB_STATUS_LABELS,
            kind_labels=PHOTO_KIND_LABELS,
        )

    @app.post("/jobs/<int:job_id>/photo")
    @login_required
    def job_photo(job_id: int):
        org = current_org()
        job = g.db.get(Job, job_id)
        if job is None or job.organization_id != org.id:
            abort(404)

        key = _store(request.files.get("photo"), "jobs")
        if key:
            kind = request.form.get("kind", PhotoKind.GENERAL)
            g.db.add(
                Photo(
                    organization_id=org.id,
                    property_id=job.property_id,
                    unit_id=job.unit_id,
                    job_id=job.id,
                    kind=kind if kind in PhotoKind.ALL else PhotoKind.GENERAL,
                    caption=request.form.get("caption", "").strip() or None,
                    taken_at=dt.datetime.now(dt.timezone.utc),
                    image_key=key,
                )
            )
            g.db.commit()
            flash("Photo added.", "ok")
        return redirect(url_for("job_detail", job_id=job.id))

    @app.post("/jobs/<int:job_id>/status")
    @login_required
    def job_status(job_id: int):
        org = current_org()
        job = g.db.get(Job, job_id)
        if job is None or job.organization_id != org.id:
            abort(404)

        status = request.form.get("status", "")
        if status in JobStatus.ALL:
            job.status = status
            if status == JobStatus.DONE and not job.completed_on:
                job.completed_on = dt.date.today()
            if status in JobStatus.OPEN_STATES:
                job.completed_on = None
            g.db.commit()
            flash(f"Marked {JOB_STATUS_LABELS[status].lower()}.", "ok")
        return redirect(url_for("job_detail", job_id=job.id))

    @app.post("/photo/<int:photo_id>/caption")
    @login_required
    def photo_caption(photo_id: int):
        org = current_org()
        photo = g.db.get(Photo, photo_id)
        if photo is None or photo.organization_id != org.id:
            abort(404)
        photo.caption = request.form.get("caption", "").strip() or None
        kind = request.form.get("kind")
        if kind in PhotoKind.ALL:
            photo.kind = kind
        g.db.commit()
        return redirect(
            url_for("job_detail", job_id=photo.job_id)
            if photo.job_id
            else url_for("properties")
        )

    # -- receipt photo on an existing expense ------------------------------

    @app.post("/expense/<int:line_id>/receipt")
    @login_required
    def expense_receipt(line_id: int):
        org = current_org()
        line = g.db.get(ExpenseLine, line_id)
        if line is None or line.organization_id != org.id:
            abort(404)

        key = _store(request.files.get("receipt"), "receipts")
        if key:
            receipt = g.db.get(Receipt, line.receipt_id) if line.receipt_id else None
            if receipt is None:
                receipt = Receipt(
                    organization_id=org.id,
                    vendor_id=line.vendor_id,
                    purchased_on=line.incurred_on,
                    total_cents=line.amount_cents,
                    image_key=key,
                )
                g.db.add(receipt)
                g.db.flush()
                line.receipt_id = receipt.id
            else:
                receipt.image_key = key
            g.db.commit()
            flash("Receipt attached.", "ok")
        return redirect(url_for("property_detail", property_id=line.property_id))

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

    # -- navigation context ------------------------------------------------

    # Which endpoint belongs under which tab. Detail pages keep their parent
    # tab lit, so you always know where you are.
    TAB_FOR_ENDPOINT = {
        "properties": "properties",
        "property_detail": "properties",
        "jobs": "repairs",
        "job_detail": "repairs",
        "job_new": "repairs",
        "expense_new": "add",
        "review": "review",
        "review_resolve": "review",
    }

    @app.context_processor
    def nav_context():
        if request.endpoint in (None, "static", "sign_in", "media"):
            return {"active_tab": None, "open_jobs": 0, "to_review": 0}

        counts = {"open_jobs": 0, "to_review": 0}
        try:
            org = current_org()
            counts["open_jobs"] = (
                g.db.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(
                        Job.organization_id == org.id,
                        Job.status.in_(list(JobStatus.OPEN_STATES)),
                    )
                )
                or 0
            )
            counts["to_review"] = (
                g.db.scalar(
                    select(func.count())
                    .select_from(ExpenseLine)
                    .where(
                        ExpenseLine.organization_id == org.id,
                        ExpenseLine.needs_review.is_(True),
                    )
                )
                or 0
            )
        except Exception:
            # A badge is never worth a 500. If the count can't be read, show
            # the tabs without it.
            pass

        return {"active_tab": TAB_FOR_ENDPOINT.get(request.endpoint), **counts}

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
    app.jinja_env.globals["job_status_label"] = lambda key: JOB_STATUS_LABELS.get(key, key)

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
