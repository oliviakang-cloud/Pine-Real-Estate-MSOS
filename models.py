"""Data model for the Pine Real Estate tracking app.

SQLAlchemy 2.0 declarative style. Runs on PostgreSQL in production and on
SQLite for tests, so no PostgreSQL-only column types are used.

Design rules this file follows:

1.  Money is integer cents everywhere (see money.py). No Float columns.
2.  Every row carries ``organization_id``. The app is being built for Pine
    first, but the same code should serve a second client without a migration.
3.  A receipt owns many expense lines. The sample Home Depot order is one
    receipt, one appliance to depreciate, ten supply lines to deduct, plus
    tax and delivery to allocate. A flat "one expense per receipt" table
    cannot represent that.
4.  Nothing is deleted. Records are closed, voided, or superseded.

Tables marked V2 are part of the agreed model but are not needed for the
first pilot; create them now so later migrations are additive only.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Controlled vocabularies
#
# Kept as plain string constants rather than database enums: adding a value
# should not require a migration, and SQLite has no native enum type.
# ---------------------------------------------------------------------------


class ScheduleE:
    """IRS Schedule E, Part I expense lines (2026 form layout)."""

    ADVERTISING = "advertising"
    AUTO_TRAVEL = "auto_travel"
    CLEANING_MAINTENANCE = "cleaning_maintenance"
    COMMISSIONS = "commissions"
    INSURANCE = "insurance"
    LEGAL_PROFESSIONAL = "legal_professional"
    MANAGEMENT_FEES = "management_fees"
    MORTGAGE_INTEREST = "mortgage_interest"
    OTHER_INTEREST = "other_interest"
    REPAIRS = "repairs"
    SUPPLIES = "supplies"
    TAXES = "taxes"
    UTILITIES = "utilities"
    OTHER = "other"
    UNCATEGORIZED = "uncategorized"

    ALL = (
        ADVERTISING, AUTO_TRAVEL, CLEANING_MAINTENANCE, COMMISSIONS,
        INSURANCE, LEGAL_PROFESSIONAL, MANAGEMENT_FEES, MORTGAGE_INTEREST,
        OTHER_INTEREST, REPAIRS, SUPPLIES, TAXES, UTILITIES, OTHER,
        UNCATEGORIZED,
    )


class CostTreatment:
    """Deduct now, or capitalize and depreciate."""

    REPAIR = "repair"
    CAPITAL = "capital"
    UNDECIDED = "undecided"
    ALL = (REPAIR, CAPITAL, UNDECIDED)


class JobStatus:
    REPORTED = "reported"
    PARTS_ORDERED = "parts_ordered"
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"
    OPEN_STATES = (REPORTED, PARTS_ORDERED, SCHEDULED, IN_PROGRESS)
    ALL = OPEN_STATES + (DONE, CANCELLED)


class PhotoKind:
    MOVE_IN = "move_in"
    MOVE_OUT = "move_out"
    BEFORE_REPAIR = "before_repair"
    AFTER_REPAIR = "after_repair"
    GENERAL = "general"
    ALL = (MOVE_IN, MOVE_OUT, BEFORE_REPAIR, AFTER_REPAIR, GENERAL)


class ExtractionStatus:
    NONE = "none"
    PENDING = "pending"
    EXTRACTED = "extracted"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    ALL = (NONE, PENDING, EXTRACTED, CONFIRMED, FAILED)


# ---------------------------------------------------------------------------
# Mixins
# ---------------------------------------------------------------------------


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class OrgMixin:
    @staticmethod
    def _org_fk() -> Mapped[int]:
        return mapped_column(ForeignKey("organization.id"), nullable=False, index=True)


# ---------------------------------------------------------------------------
# Tenancy and ownership
# ---------------------------------------------------------------------------


class Organization(Base, TimestampMixin):
    """One client of the nonprofit. Pine is the first."""

    __tablename__ = "organization"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    default_tax_rate_bp: Mapped[Optional[int]] = mapped_column(Integer)  # basis points
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    entities: Mapped[list["Entity"]] = relationship(back_populates="organization")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Organization {self.slug}>"


class Entity(Base, TimestampMixin):
    """Legal owner of properties: a trust, an LLC, or an individual.

    Taxes are filed per entity, so exports group here first.
    """

    __tablename__ = "entity"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="llc", nullable=False)
    tax_year_end: Mapped[str] = mapped_column(String(5), default="12-31", nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    organization: Mapped[Organization] = relationship(back_populates="entities")
    properties: Mapped[list["Property"]] = relationship(back_populates="entity")


class Member(Base, TimestampMixin):
    """A person who can pay for things: an owner, a partner, or a helper.

    ``ExpenseLine.paid_by_member_id`` plus ``reimbursed`` is what answers the
    question that started this project: who fronted money, and who owes whom.
    """

    __tablename__ = "member"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(160))
    ownership_bp: Mapped[Optional[int]] = mapped_column(Integer)  # 5000 = 50%
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Property(Base, TimestampMixin):
    __tablename__ = "property"
    __table_args__ = (
        UniqueConstraint("organization_id", "street"),
        Index("ix_property_org_active", "organization_id", "active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id"), nullable=False, index=True
    )
    entity_id: Mapped[Optional[int]] = mapped_column(ForeignKey("entity.id"), index=True)

    street: Mapped[str] = mapped_column(String(200), nullable=False)
    city: Mapped[Optional[str]] = mapped_column(String(80))
    state: Mapped[Optional[str]] = mapped_column(String(2))
    postal_code: Mapped[Optional[str]] = mapped_column(String(10))
    county: Mapped[Optional[str]] = mapped_column(String(60))

    # Nickname used on receipts and in conversation, e.g. "Frisbie Ct".
    # The importer and the receipt reader both match on this.
    short_name: Mapped[Optional[str]] = mapped_column(String(60), index=True)
    tax_rate_bp: Mapped[Optional[int]] = mapped_column(Integer)
    acquired_on: Mapped[Optional[dt.date]] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    entity: Mapped[Optional[Entity]] = relationship(back_populates="properties")
    units: Mapped[list["Unit"]] = relationship(
        back_populates="property", cascade="all, delete-orphan"
    )

    @property
    def label(self) -> str:
        return self.short_name or self.street

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Property {self.street}>"


class Unit(Base, TimestampMixin):
    """A rentable unit.

    Single-family properties get one implicit unit (``is_whole_property``), so
    leases and rent always hang off a unit and reports never special-case them.
    Building-level costs attach to the property with ``unit_id`` NULL.
    """

    __tablename__ = "unit"
    __table_args__ = (UniqueConstraint("property_id", "label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id"), nullable=False, index=True
    )
    property_id: Mapped[int] = mapped_column(
        ForeignKey("property.id"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(40), nullable=False)  # "Unit 3"
    is_whole_property: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    bedrooms: Mapped[Optional[int]] = mapped_column(Integer)
    bathrooms_x10: Mapped[Optional[int]] = mapped_column(Integer)  # 15 = 1.5 baths

    property: Mapped[Property] = relationship(back_populates="units")


# ---------------------------------------------------------------------------
# Vendors and the SKU dictionary
# ---------------------------------------------------------------------------


class Vendor(Base, TimestampMixin):
    __tablename__ = "vendor"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="retailer", nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(30))
    email: Mapped[Optional[str]] = mapped_column(String(160))
    w9_on_file: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Labor vendors need 1099 totals; retailers do not.
    track_1099: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)


class SkuItem(Base, TimestampMixin):
    """Learned description and category for a vendor SKU.

    Nine of the twelve lines on the sample Home Depot order carry no
    description, only a SKU. Name a SKU once here and every later receipt
    fills itself in.
    """

    __tablename__ = "sku_item"
    __table_args__ = (UniqueConstraint("organization_id", "vendor_id", "sku"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id"), nullable=False, index=True
    )
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendor.id"), nullable=False)
    sku: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(300))
    default_category: Mapped[str] = mapped_column(
        String(30), default=ScheduleE.UNCATEGORIZED, nullable=False
    )
    default_treatment: Mapped[str] = mapped_column(
        String(12), default=CostTreatment.REPAIR, nullable=False
    )
    times_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_unit_price_cents: Mapped[Optional[int]] = mapped_column(Integer)


# ---------------------------------------------------------------------------
# Jobs (repair and maintenance items)
# ---------------------------------------------------------------------------


class Job(Base, TimestampMixin):
    """A repair or maintenance item with a lifecycle.

    This is the "what is open right now" table. Expenses point at it, so a job
    can report its own total cost.
    """

    __tablename__ = "job"
    __table_args__ = (
        CheckConstraint(
            "status in ('reported','parts_ordered','scheduled','in_progress','done','cancelled')",
            name="ck_job_status",
        ),
        Index("ix_job_open", "organization_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id"), nullable=False, index=True
    )
    property_id: Mapped[int] = mapped_column(
        ForeignKey("property.id"), nullable=False, index=True
    )
    unit_id: Mapped[Optional[int]] = mapped_column(ForeignKey("unit.id"), index=True)

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20), default=JobStatus.REPORTED, nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, default=3, nullable=False)  # 1 = urgent
    reported_on: Mapped[Optional[dt.date]] = mapped_column(Date)
    scheduled_for: Mapped[Optional[dt.date]] = mapped_column(Date)
    completed_on: Mapped[Optional[dt.date]] = mapped_column(Date)
    vendor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("vendor.id"))
    tenant_chargeable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    move_out_id: Mapped[Optional[int]] = mapped_column(ForeignKey("move_out.id"))

    lines: Mapped[list["ExpenseLine"]] = relationship(back_populates="job")

    @property
    def is_open(self) -> bool:
        return self.status in JobStatus.OPEN_STATES

    def total_cents(self) -> int:
        return sum(line.amount_cents for line in self.lines)


# ---------------------------------------------------------------------------
# Receipts and expense lines
# ---------------------------------------------------------------------------


class Receipt(Base, TimestampMixin):
    """A single purchase document: register receipt, online order, or invoice.

    Header totals are stored exactly as printed. ``imbalance_cents`` must be
    zero before a receipt is considered confirmed, which is what keeps
    allocated tax and discounts honest.
    """

    __tablename__ = "receipt"
    __table_args__ = (
        UniqueConstraint("organization_id", "vendor_id", "order_number"),
        Index("ix_receipt_date", "organization_id", "purchased_on"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id"), nullable=False, index=True
    )
    vendor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("vendor.id"), index=True)

    order_number: Mapped[Optional[str]] = mapped_column(String(60))
    invoice_number: Mapped[Optional[str]] = mapped_column(String(60))
    # Home Depot's PO/Job Name field: "Frisbie Ct Unit 5". Filling this in at
    # checkout is what lets extraction assign the property with confidence.
    po_job_name: Mapped[Optional[str]] = mapped_column(String(120))

    purchased_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    subtotal_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discount_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tax_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    shipping_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    payment_method: Mapped[Optional[str]] = mapped_column(String(40))  # "HD-1984"
    paid_by_member_id: Mapped[Optional[int]] = mapped_column(ForeignKey("member.id"))

    image_key: Mapped[Optional[str]] = mapped_column(String(300))  # object storage key
    extraction_status: Mapped[str] = mapped_column(
        String(12), default=ExtractionStatus.NONE, nullable=False
    )
    extraction_confidence: Mapped[Optional[int]] = mapped_column(Integer)  # 0-100
    confirmed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["ExpenseLine"]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan"
    )

    def imbalance_cents(self) -> int:
        """Line totals minus the printed total. Must be 0.

        Lines are stored tax-inclusive, so the check is simply: do the lines
        add up to what was charged?
        """
        return sum(line.amount_cents for line in self.lines) - self.total_cents

    def header_imbalance_cents(self) -> int:
        """Printed subtotal arithmetic check, independent of the lines."""
        computed = (
            self.subtotal_cents
            - self.discount_cents
            + self.tax_cents
            + self.shipping_cents
        )
        return computed - self.total_cents

    @property
    def is_balanced(self) -> bool:
        return self.imbalance_cents() == 0


class ExpenseLine(Base, TimestampMixin):
    """One categorized cost against one property (and optionally one unit).

    Amounts are tax-inclusive: ``amount_cents`` is what the money actually
    cost. ``pretax_cents`` and ``tax_cents`` are kept alongside for audit and
    for the Schedule E export.
    """

    __tablename__ = "expense_line"
    __table_args__ = (
        CheckConstraint(
            "treatment in ('repair','capital','undecided')", name="ck_expense_treatment"
        ),
        # Re-running the importer must not duplicate rows.
        UniqueConstraint("organization_id", "source_ref", name="uq_expense_source_ref"),
        Index("ix_expense_property_date", "property_id", "incurred_on"),
        Index("ix_expense_review", "organization_id", "needs_review"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id"), nullable=False, index=True
    )
    property_id: Mapped[int] = mapped_column(
        ForeignKey("property.id"), nullable=False, index=True
    )
    unit_id: Mapped[Optional[int]] = mapped_column(ForeignKey("unit.id"), index=True)
    receipt_id: Mapped[Optional[int]] = mapped_column(ForeignKey("receipt.id"), index=True)
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("job.id"), index=True)
    vendor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("vendor.id"), index=True)

    incurred_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(String(400), nullable=False)
    sku: Mapped[Optional[str]] = mapped_column(String(40))
    quantity_x100: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    pretax_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tax_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discount_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    tax_rate_bp: Mapped[Optional[int]] = mapped_column(Integer)  # 1025 = 10.25%

    category: Mapped[str] = mapped_column(
        String(30), default=ScheduleE.UNCATEGORIZED, nullable=False
    )
    treatment: Mapped[str] = mapped_column(
        String(12), default=CostTreatment.UNDECIDED, nullable=False
    )
    tenant_chargeable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    paid_by_member_id: Mapped[Optional[int]] = mapped_column(ForeignKey("member.id"))
    reimbursed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reimbursed_on: Mapped[Optional[dt.date]] = mapped_column(Date)

    # Provenance. "spreadsheet:2026-09:Sheet1!E24" or "receipt:1052" so a row
    # can always be traced back and the import can be re-run safely.
    source: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    source_ref: Mapped[Optional[str]] = mapped_column(String(120))
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    review_note: Mapped[Optional[str]] = mapped_column(String(300))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    receipt: Mapped[Optional[Receipt]] = relationship(back_populates="lines")
    job: Mapped[Optional[Job]] = relationship(back_populates="lines")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ExpenseLine {self.incurred_on} {self.amount_cents}c {self.description[:30]!r}>"


# ---------------------------------------------------------------------------
# Tenancy, rent, move-out  (V2: modelled now, built after the expense pilot)
# ---------------------------------------------------------------------------


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenant"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(30))
    email: Mapped[Optional[str]] = mapped_column(String(160))
    forwarding_address: Mapped[Optional[str]] = mapped_column(String(300))


class Lease(Base, TimestampMixin):
    __tablename__ = "lease"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id"), nullable=False, index=True
    )
    unit_id: Mapped[int] = mapped_column(ForeignKey("unit.id"), nullable=False, index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"), nullable=False)
    starts_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[Optional[dt.date]] = mapped_column(Date)
    rent_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    # Section 8: the housing authority portion is tracked separately so a
    # partial tenant payment is not flagged as a shortfall.
    subsidy_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deposit_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class RentPayment(Base, TimestampMixin):
    __tablename__ = "rent_payment"
    __table_args__ = (Index("ix_rent_lease_period", "lease_id", "period_start"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id"), nullable=False, index=True
    )
    lease_id: Mapped[int] = mapped_column(ForeignKey("lease.id"), nullable=False)
    period_start: Mapped[dt.date] = mapped_column(Date, nullable=False)
    expected_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    received_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    received_on: Mapped[Optional[dt.date]] = mapped_column(Date)
    method: Mapped[Optional[str]] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(20), default="tenant", nullable=False)


class MoveOut(Base, TimestampMixin):
    """Deposit accounting for one ending tenancy.

    ``statement_due_on`` is set from the property's state: 21 calendar days in
    California, 14 business days in Arizona. The app surfaces the countdown;
    the final legal call stays with the landlord.
    """

    __tablename__ = "move_out"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id"), nullable=False, index=True
    )
    lease_id: Mapped[int] = mapped_column(ForeignKey("lease.id"), nullable=False)
    notice_received_on: Mapped[Optional[dt.date]] = mapped_column(Date)
    pre_inspection_offered_on: Mapped[Optional[dt.date]] = mapped_column(Date)
    pre_inspection_done_on: Mapped[Optional[dt.date]] = mapped_column(Date)
    moved_out_on: Mapped[Optional[dt.date]] = mapped_column(Date)
    statement_due_on: Mapped[Optional[dt.date]] = mapped_column(Date, index=True)
    statement_sent_on: Mapped[Optional[dt.date]] = mapped_column(Date)
    forwarding_address: Mapped[Optional[str]] = mapped_column(String(300))
    deposit_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    refund_cents: Mapped[Optional[int]] = mapped_column(Integer)
    keys_returned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    items: Mapped[list["MoveOutItem"]] = relationship(back_populates="move_out")


class MoveOutItem(Base, TimestampMixin):
    """One room-level finding, chargeable or not."""

    __tablename__ = "move_out_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id"), nullable=False, index=True
    )
    move_out_id: Mapped[int] = mapped_column(
        ForeignKey("move_out.id"), nullable=False, index=True
    )
    room: Mapped[str] = mapped_column(String(60), nullable=False)
    finding: Mapped[str] = mapped_column(String(400), nullable=False)
    chargeable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Wear-and-tear reasoning, and useful-life proration for carpet and paint.
    wear_and_tear_note: Mapped[Optional[str]] = mapped_column(String(300))
    useful_life_years: Mapped[Optional[int]] = mapped_column(Integer)
    age_years_x10: Mapped[Optional[int]] = mapped_column(Integer)
    charge_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expense_line_id: Mapped[Optional[int]] = mapped_column(ForeignKey("expense_line.id"))

    move_out: Mapped[MoveOut] = relationship(back_populates="items")


class Photo(Base, TimestampMixin):
    __tablename__ = "photo"
    __table_args__ = (Index("ix_photo_subject", "property_id", "unit_id", "kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id"), nullable=False, index=True
    )
    property_id: Mapped[int] = mapped_column(ForeignKey("property.id"), nullable=False)
    unit_id: Mapped[Optional[int]] = mapped_column(ForeignKey("unit.id"))
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("job.id"))
    move_out_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("move_out_item.id"))
    kind: Mapped[str] = mapped_column(String(20), default=PhotoKind.GENERAL, nullable=False)
    room: Mapped[Optional[str]] = mapped_column(String(60))
    taken_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    image_key: Mapped[str] = mapped_column(String(300), nullable=False)
    caption: Mapped[Optional[str]] = mapped_column(String(300))


class Attachment(Base, TimestampMixin):
    """Any other document: lease PDF, W-9, invoice, inspection report."""

    __tablename__ = "attachment"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id"), nullable=False, index=True
    )
    subject_type: Mapped[str] = mapped_column(String(30), nullable=False)
    subject_id: Mapped[int] = mapped_column(Integer, nullable=False)
    file_key: Mapped[str] = mapped_column(String(300), nullable=False)
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    content_type: Mapped[Optional[str]] = mapped_column(String(80))
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
