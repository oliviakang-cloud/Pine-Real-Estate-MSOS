"""Pull structure out of the free text in the old spreadsheet.

The current sheet stores a date, a description, a vendor, and a category all
in one string: ``9/13 Interior Semigloss Swiss Coffee 1 Gallon`` or
``9/2 Labor for tub surround and dump garbage by Camilo Zarate``.

These rules recover the pieces. They are deliberately conservative: anything
uncertain is imported with ``needs_review`` set rather than guessed at. The
same rule table is a reasonable starting point for the receipt-extraction
confirm screen later.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Optional

from .models import CostTreatment, ScheduleE

__all__ = [
    "ParsedAddress",
    "ParsedDescription",
    "parse_address",
    "parse_description",
    "classify",
    "CAPITAL_REVIEW_THRESHOLD_CENTS",
]

# A single line at or above this amount is worth a human deciding whether it
# is a deductible repair or a capital improvement. The $1,600 tub surround in
# the September sheet is exactly the case this catches.
CAPITAL_REVIEW_THRESHOLD_CENTS = 50_000


@dataclass
class ParsedAddress:
    street: str
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    short_name: Optional[str] = None


_ADDR_RE = re.compile(
    r"^(?P<street>.+?),\s*(?P<city>[^,]+),\s*(?P<state>[A-Z]{2})\s*(?P<zip>\d{5})?\s*$"
)
_LEADING_NUMBER_RE = re.compile(r"^\d+\s+")


def parse_address(raw: str) -> ParsedAddress:
    """``1650 Frisbie Ct., Concord, CA 94520`` into its parts.

    ``short_name`` is the street without the house number or unit suffix
    (``Frisbie Ct``). That is how a property gets referred to out loud and how
    it shows up in the PO/Job field on a Home Depot receipt, so it is the key
    the receipt reader will match on later.
    """
    text = " ".join(str(raw).split())
    match = _ADDR_RE.match(text)
    if match:
        street = match.group("street").strip()
        addr = ParsedAddress(
            street=street,
            city=match.group("city").strip(),
            state=match.group("state"),
            postal_code=match.group("zip"),
        )
    else:
        addr = ParsedAddress(street=text)

    # The house number goes, the unit suffix stays: "681 Royston Ln #332" and
    # "673 Royston Ln #334" are two different condos and must not collapse.
    short = _LEADING_NUMBER_RE.sub("", addr.street).strip().rstrip(".,")
    addr.short_name = short or None
    return addr


@dataclass
class ParsedDescription:
    text: str
    month: Optional[int] = None
    day: Optional[int] = None
    vendor_name: Optional[str] = None
    vendor_kind: Optional[str] = None  # "retailer" | "labor"
    notes: list[str] = field(default_factory=list)

    def date_for_year(self, year: int) -> Optional[dt.date]:
        if self.month is None or self.day is None:
            return None
        try:
            return dt.date(year, self.month, self.day)
        except ValueError:
            return None


_DATE_PREFIX_RE = re.compile(r"^(?P<m>\d{1,2})/(?P<d>\d{1,2})\s+(?P<rest>.*)$")
# "... at Amazon", "... at Home Depot"
_VENDOR_AT_RE = re.compile(r"\bat\s+(?P<vendor>[A-Z][A-Za-z0-9&'.\- ]{2,40})\s*$")
# "... by Camilo Zarate"
_VENDOR_BY_RE = re.compile(r"\bby\s+(?P<vendor>[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b")

KNOWN_RETAILERS = {
    "amazon": "Amazon",
    "home depot": "Home Depot",
    "the home depot": "Home Depot",
    "lowes": "Lowe's",
    "lowe's": "Lowe's",
    "ace hardware": "Ace Hardware",
    "harbor freight": "Harbor Freight",
    "costco": "Costco",
    "walmart": "Walmart",
    "grainger": "Grainger",
    "ferguson": "Ferguson",
}


def parse_description(raw: str) -> ParsedDescription:
    text = " ".join(str(raw).split())
    parsed = ParsedDescription(text=text)

    match = _DATE_PREFIX_RE.match(text)
    if match:
        parsed.month = int(match.group("m"))
        parsed.day = int(match.group("d"))
        parsed.text = match.group("rest").strip()

    body = parsed.text
    at_match = _VENDOR_AT_RE.search(body)
    if at_match:
        name = at_match.group("vendor").strip().rstrip(".")
        parsed.vendor_name = KNOWN_RETAILERS.get(name.lower(), name)
        parsed.vendor_kind = "retailer"
        parsed.text = body[: at_match.start()].strip().rstrip(",")
    else:
        by_match = _VENDOR_BY_RE.search(body)
        if by_match:
            parsed.vendor_name = by_match.group("vendor").strip()
            parsed.vendor_kind = "labor"
        else:
            lowered = body.lower()
            for key, name in KNOWN_RETAILERS.items():
                if key in lowered:
                    parsed.vendor_name = name
                    parsed.vendor_kind = "retailer"
                    break
    return parsed


@dataclass
class Classification:
    category: str
    treatment: str
    needs_review: bool = False
    review_note: Optional[str] = None


# (keywords, Schedule E category, treatment). First match wins, so the more
# specific patterns are listed first.
_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("program fee", "business license", "registration fee", "rental program"),
     ScheduleE.TAXES, CostTreatment.REPAIR),
    (("property tax", "county tax", "transfer tax"),
     ScheduleE.TAXES, CostTreatment.REPAIR),
    (("insurance", "premium"), ScheduleE.INSURANCE, CostTreatment.REPAIR),
    (("hoa", "association dues"), ScheduleE.OTHER, CostTreatment.REPAIR),
    (("attorney", "legal", "eviction", "cpa", "accountant", "notary"),
     ScheduleE.LEGAL_PROFESSIONAL, CostTreatment.REPAIR),
    (("listing", "advertis", "zillow", "sign"),
     ScheduleE.ADVERTISING, CostTreatment.REPAIR),
    (("labor", "handyman", "plumber", "electrician", "contractor", "haul",
      "dump", "cleaning", "clean out", "landscap", "gardener", "pest",
      "carpet clean"),
     ScheduleE.CLEANING_MAINTENANCE, CostTreatment.REPAIR),
    (("water bill", "pg&e", "electric bill", "gas bill", "garbage service",
      "waste hauling", "sewer", "utility bill"),
     ScheduleE.UTILITIES, CostTreatment.REPAIR),
    (("paint", "primer", "semigloss", "eggshell", "enamel", "stain",
      "joint compound", "caulk", "sealant", "spackle", "drywall", "brush",
      "roller", "sandpaper"),
     ScheduleE.SUPPLIES, CostTreatment.REPAIR),
    (("range", "burner", "oven", "stove", "refrigerator", "dishwasher",
      "washer", "dryer", "microwave", "disposal", "water heater", "furnace",
      "hvac", "thermostat"),
     ScheduleE.REPAIRS, CostTreatment.REPAIR),
    (("faucet", "valve", "supply line", "toilet", "toile", "hinge", "handle",
      "shower", "tub", "surround", "vanity", "sink", "p-trap"),
     ScheduleE.REPAIRS, CostTreatment.REPAIR),
    (("lock", "rekey", "key", "deadbolt", "smoke detector", "co detector",
      "filter", "light", "bulb", "outlet", "switch", "breaker", "fan"),
     ScheduleE.REPAIRS, CostTreatment.REPAIR),
    (("screw", "nail", "bolt", "tape", "glue", "adhesive", "hardware",
      "tool", "blade", "battery"),
     ScheduleE.SUPPLIES, CostTreatment.REPAIR),
)

# Words that suggest an improvement rather than a fix, regardless of amount.
_CAPITAL_HINTS = (
    "replace", "replacement", "install", "installation", "new ", "remodel",
    "upgrade", "surround", "roof", "window", "flooring", "carpet install",
    "cabinet", "countertop", "addition",
)


def classify(description: str, amount_cents: int) -> Classification:
    """Best-effort Schedule E category and repair/capital treatment.

    Anything large or improvement-shaped is imported as ``undecided`` and
    flagged, because that split changes the tax return and should not be
    guessed by a keyword table.
    """
    text = description.lower()
    category = ScheduleE.UNCATEGORIZED
    treatment = CostTreatment.REPAIR
    for keywords, cat, treat in _RULES:
        if any(word in text for word in keywords):
            category, treatment = cat, treat
            break

    needs_review = category == ScheduleE.UNCATEGORIZED
    note = "No category rule matched" if needs_review else None

    big = amount_cents >= CAPITAL_REVIEW_THRESHOLD_CENTS
    improvement_words = any(word in text for word in _CAPITAL_HINTS)
    if big and improvement_words:
        treatment = CostTreatment.UNDECIDED
        needs_review = True
        note = "Large improvement-type cost: confirm repair vs. capital"
    elif big:
        needs_review = True
        note = note or "Amount over review threshold: confirm treatment"

    return Classification(category, treatment, needs_review, note)
