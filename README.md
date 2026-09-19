# Pine Tracker: data model and spreadsheet importer

Step 1 of the build plan. The schema, plus a tool that loads the existing
monthly expense workbooks into it so the app launches with real history.

## Files

| File | What it is |
| --- | --- |
| `models.py` | SQLAlchemy 2.0 models. Runs on PostgreSQL in production, SQLite in tests. |
| `money.py` | Integer-cents helpers and the largest-remainder allocator. |
| `formula.py` | Decomposes spreadsheet cell formulas into pre-tax amount + tax rate. |
| `classify.py` | Address parsing, vendor extraction, Schedule E category rules. |
| `import_expenses.py` | The importer and its CLI report. |
| `tests/test_import.py` | 27 tests, including an end-to-end import of the real workbook. |
| `sample_data/` | The September 2026 workbook, used as a test fixture. |

## Running it

```bash
pip install sqlalchemy openpyxl pytest

# parse and report without writing anything
python -m pine_tracker.import_expenses \
    --file pine_tracker/sample_data/Sample_Operating_Expenses_Tracking_for_September_2026.xlsx \
    --db "sqlite:///pine.db" \
    --entity "Kang Family Revocable Trust" \
    --dry-run

# several months at once, then commit
python -m pine_tracker.import_expenses --file sheets/*.xlsx --db "$DATABASE_URL"

python -m pytest pine_tracker/tests -q
```

## What the September workbook produced

```
1650 Frisbie Ct.                 $34.79      cleaning_maintenance   $1,600.00
1650 Frisbie Ct. / Unit 1        $30.76      repairs                  $164.00
1650 Frisbie Ct. / Unit 2        $56.22      supplies                  $83.73
1650 Frisbie Ct. / Unit 3     $1,642.23      taxes                    $314.00
5130 Prewett Ranch Dr.           $59.43
654 Country Ln                   $24.30      12 cells -> 15 expense lines
673 Royston Ln #334             $157.00     13 properties, 6 units, 3 vendors
681 Royston Ln #332             $157.00      1 line flagged for review
TOTAL                         $2,161.73      sheet total $2,161.72 (1c rounding)
```

## Scope decision: all properties, not a pilot subset

The parser is generic, so one property costs the same work as all of them, and
the whole sheet is what exercises every shape in the data at once: property
rows, unit rows under the 6-unit building, cells covering two purchases, and a
labor line. Limiting the import would have hidden three of those four cases.
The same run should be repeated over the other monthly workbooks; the importer
accepts several files in one call and is safe to re-run.

## Decisions worth knowing

**Money is integer cents.** Cells such as `=1.17*1.1025+6.28*1.0875` evaluate
to 8.1194249999999997. Values are rounded to cents once, at import, with
ROUND_HALF_UP. Nothing downstream ever sees a float.

**Formulas are data, not noise.** Each cell is parsed with Python's `ast`
module (never `eval`) and split on `+`. A term like `9.88*1.1025` yields a
$9.88 pre-tax amount at a 10.25% rate; `1500+100` yields two separate lines.
That is how 12 cells became 15 expense lines, with the 10.25%, 9.75%, and
8.75% district rates all recovered. If a cell cannot be parsed, the importer
falls back to the cached value and flags the row rather than failing.

**Nothing important is guessed.** The category rules are keyword-based and
conservative. Any line over $500 that also looks like an improvement is
imported as `undecided` and flagged, because the repair/capital split changes
the tax return. In the sample that correctly caught the $1,500 tub surround
labor and left the $100 dump fee alone.

**Re-running is safe.** Every line carries a `source_ref` such as
`spreadsheet:2026-09:Sheet1!E24#1`, unique per organization. A second run
creates nothing and reports the rows it skipped.

**Two condos in the same complex stay separate.** 681 Royston Ln #332 and
673 Royston Ln #334 keep their unit suffixes. The `short_name` field drops
only the house number, which is what makes `Frisbie Ct` match the
`PO/Job Name: Frisbie Ct Unit 5` field on the sample Home Depot receipt.

**Owning entity is not in the sheet.** Pass `--entity` to assign one, or leave
it off and set it per property later. The importer does not invent it.

## Fields the spreadsheet never had

These are now first-class columns, and they are the reason to move off the
sheet at all: vendor, Schedule E category, repair vs. capital, job link,
receipt link, who paid, and reimbursed yes/no. The import fills the first
three where it can and flags the rest.

## Next

1. Seed `Member` rows for the two Pine members and backfill `paid_by_member_id`.
2. Flask app over this schema: property list, property detail, expense entry.
3. Receipt model is already in place; wire the upload plus vision extraction
   into a confirm screen, using `SkuItem` to learn SKU descriptions.
4. Schedule E export grouped by entity, with repairs and capital separated.
