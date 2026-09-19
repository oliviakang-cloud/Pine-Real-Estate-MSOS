"""Tests for the importer and its helpers.

Run from the directory above the package:

    python -m pytest pine_tracker/tests -q
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from pine_tracker.classify import classify, parse_address, parse_description
from pine_tracker.formula import ParseError, parse_amount_cell
from pine_tracker.import_expenses import import_workbook, period_from_filename
from pine_tracker.models import (
    Base,
    CostTreatment,
    ExpenseLine,
    Property,
    ScheduleE,
    Unit,
    Vendor,
)
from pine_tracker.money import allocate_cents, fmt, to_cents

SAMPLE = (
    Path(__file__).resolve().parents[1]
    / "sample_data"
    / "Sample_Operating_Expenses_Tracking_for_September_2026.xlsx"
)


# ---------------------------------------------------------------------------
# money
# ---------------------------------------------------------------------------


def test_to_cents_rounds_half_up_and_ignores_float_noise():
    assert to_cents("8.119425") == 812
    assert to_cents(8.1194249999999997) == 812
    assert to_cents("0.005") == 1
    assert to_cents(157) == 15700


def test_allocation_never_loses_a_penny():
    # $90.31 of sales tax spread across the Home Depot order lines.
    weights = [55711, 3998, 5000, 1750, 1249, 898, 1121, 4678, 3598, 7198, 3958, 3148]
    shares = allocate_cents(9031, weights)
    assert sum(shares) == 9031
    assert all(share >= 0 for share in shares)


def test_allocation_handles_zero_weights_and_negatives():
    assert sum(allocate_cents(100, [0, 0, 0])) == 100
    assert sum(allocate_cents(-500, [1, 1, 1])) == -500


def test_fmt():
    assert fmt(105238) == "$1,052.38"
    assert fmt(-1200) == "-$12.00"


# ---------------------------------------------------------------------------
# formula decomposition
# ---------------------------------------------------------------------------


def test_single_item_with_tax():
    (component,) = parse_amount_cell("=9.88*1.1025")
    assert component.pretax_cents == 988
    assert component.tax_rate_bp == 1025
    assert component.amount_cents == 1089
    assert component.tax_cents == 101


def test_two_items_two_tax_rates_in_one_cell():
    first, second = parse_amount_cell("=1.17*1.1025+6.28*1.0875")
    assert (first.pretax_cents, first.tax_rate_bp) == (117, 1025)
    assert (second.pretax_cents, second.tax_rate_bp) == (628, 875)
    assert first.amount_cents + second.amount_cents == 812


def test_parenthesised_base_is_taxed_as_one_item():
    (component,) = parse_amount_cell("=(14.38+0.3)*1.1025")
    assert component.pretax_cents == 1468
    assert component.amount_cents == 1618  # 14.68 * 1.1025 = 16.1847


def test_untaxed_addition_splits_into_two_components():
    components = parse_amount_cell("=17.3+17.49")
    assert [c.amount_cents for c in components] == [1730, 1749]
    assert all(c.tax_rate_bp is None for c in components)


def test_labor_plus_fee_splits():
    components = parse_amount_cell("=1500+100")
    assert [c.amount_cents for c in components] == [150000, 10000]


def test_quantity_times_price_is_not_read_as_tax():
    (component,) = parse_amount_cell("=3*12.99")
    assert component.amount_cents == 3897
    assert component.tax_rate_bp is None


def test_plain_number_and_empty_cell():
    (component,) = parse_amount_cell(157)
    assert component.amount_cents == 15700
    assert parse_amount_cell(None) == []
    assert parse_amount_cell("  ") == []


def test_cell_reference_raises_so_caller_can_fall_back():
    with pytest.raises(ParseError):
        parse_amount_cell("=SUM(E3:E9)")


# ---------------------------------------------------------------------------
# text parsing and classification
# ---------------------------------------------------------------------------


def test_address_parsing_keeps_condos_distinct():
    a = parse_address("681 Royston Ln #332, Hayward, CA 94544")
    b = parse_address("673 Royston Ln #334, Hayward, CA 94544")
    assert a.city == "Hayward" and a.state == "CA" and a.postal_code == "94544"
    assert a.short_name != b.short_name


def test_address_short_name_matches_receipt_job_field():
    parsed = parse_address("1650 Frisbie Ct., Concord, CA 94520")
    # Home Depot's PO/Job Name on the sample receipt reads "Frisbie Ct Unit 5".
    assert parsed.short_name == "Frisbie Ct"


def test_description_splits_date_vendor_and_text():
    parsed = parse_description("9/15 Range Switch WB24T10022 for GE 6\" Burner at Amazon")
    assert (parsed.month, parsed.day) == (9, 15)
    assert parsed.vendor_name == "Amazon"
    assert parsed.vendor_kind == "retailer"
    assert "at Amazon" not in parsed.text


def test_labor_vendor_is_a_person():
    parsed = parse_description("9/2 Labor for tub surround and dump garbage by Camilo Zarate")
    assert parsed.vendor_name == "Camilo Zarate"
    assert parsed.vendor_kind == "labor"


def test_category_rules():
    assert classify("Hayward Rental Program Fee", 15700).category == ScheduleE.TAXES
    assert classify("Interior Semigloss Swiss Coffee 1 Gallon", 4854).category == ScheduleE.SUPPLIES
    assert classify("Range Control Switch for GE Burner", 1730).category == ScheduleE.REPAIRS
    labor = classify("Labor for tub surround and dump garbage", 150000)
    assert labor.category == ScheduleE.CLEANING_MAINTENANCE


def test_large_improvement_is_flagged_not_guessed():
    verdict = classify("Labor for tub surround and dump garbage", 150000)
    assert verdict.treatment == CostTreatment.UNDECIDED
    assert verdict.needs_review


def test_small_repair_is_not_flagged():
    verdict = classify("Toile seat hinge", 329)
    assert not verdict.needs_review
    assert verdict.treatment == CostTreatment.REPAIR


def test_period_from_filename():
    assert period_from_filename(
        Path("Sample_Operating_Expenses_Tracking_for_September_2026.xlsx")
    ) == (2026, 9)


# ---------------------------------------------------------------------------
# end-to-end import of the real workbook
# ---------------------------------------------------------------------------


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture()
def imported(session):
    stats = import_workbook(session, SAMPLE, entity_name="Kang Family Revocable Trust")
    return session, stats


def test_import_ties_out_to_the_spreadsheet(imported):
    session, stats = imported
    lines = session.scalars(select(ExpenseLine)).all()
    total = sum(line.amount_cents for line in lines)
    # Per-item rounding differs from the sheet's per-row rounding by at most a
    # cent per row; anything larger means the parser dropped something.
    assert abs(total - stats.sheet_total_cents) <= len(lines)
    assert total == 216173


def test_multi_item_cells_became_multiple_lines(imported):
    session, stats = imported
    assert stats.entries == 12
    assert stats.lines_created == 15


def test_units_attach_to_the_right_building(imported):
    session, _ = imported
    frisbie = session.scalar(select(Property).where(Property.short_name == "Frisbie Ct"))
    units = session.scalars(select(Unit).where(Unit.property_id == frisbie.id)).all()
    # All six units register, including the three with no September spending.
    assert {unit.label for unit in units} == {
        "Unit 1", "Unit 2", "Unit 3", "Unit 4", "Unit 5", "Unit 6"
    }

    # The building-level range switches belong to the property, not a unit.
    building = session.scalars(
        select(ExpenseLine).where(
            ExpenseLine.property_id == frisbie.id, ExpenseLine.unit_id.is_(None)
        )
    ).all()
    assert sum(line.amount_cents for line in building) == 3479


def test_vendors_created_with_1099_flag(imported):
    session, _ = imported
    names = {vendor.name: vendor for vendor in session.scalars(select(Vendor)).all()}
    assert set(names) == {"Amazon", "Home Depot", "Camilo Zarate"}
    assert names["Camilo Zarate"].track_1099 is True
    assert names["Amazon"].track_1099 is False


def test_tax_rates_are_recovered_per_line(imported):
    session, _ = imported
    rates = {
        line.tax_rate_bp
        for line in session.scalars(select(ExpenseLine)).all()
        if line.tax_rate_bp
    }
    assert rates == {1025, 875, 975}  # 10.25%, 8.75%, 9.75% districts


def test_everything_is_categorized(imported):
    session, _ = imported
    uncategorized = session.scalars(
        select(ExpenseLine).where(ExpenseLine.category == ScheduleE.UNCATEGORIZED)
    ).all()
    assert uncategorized == []


def test_reimport_is_idempotent(session):
    first = import_workbook(session, SAMPLE)
    session.commit()
    second = import_workbook(session, SAMPLE)
    assert second.lines_created == 0
    assert second.lines_skipped == first.lines_created


def test_whole_portfolio_registers_even_without_spending(imported):
    """Addresses with no September activity still become property records."""
    session, stats = imported
    assert stats.properties_created == 13
    quiet = session.scalar(
        select(Property).where(Property.street.like("%Tupelo%"))
    )
    assert quiet is not None
    spend = session.scalars(
        select(ExpenseLine).where(ExpenseLine.property_id == quiet.id)
    ).all()
    assert spend == []
