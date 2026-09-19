"""Decompose the arithmetic in the existing expense spreadsheet.

The cells in Wei's workbook are not plain numbers. They look like this:

    =1.17*1.1025+6.28*1.0875      two items, two different tax rates
    =(14.38+0.3)*1.1025           item plus a CRV/fee, taxed together
    =9.88*1.1025                  one item
    =17.3+17.49                   two untaxed items (Amazon, tax included)
    =1500+100                     labor plus a dump fee

That is real information: the pre-tax price of each item, the tax rate that
applied, and the fact that one cell often covers more than one purchase.
Importing only the computed float (8.1194249999999997) would throw it away.

This module parses the formula with Python's ``ast`` module (no ``eval``) and
returns one component per addend.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .money import to_cents

__all__ = ["Component", "parse_amount_cell", "ParseError"]

# A multiplier inside this range is read as "price times one plus tax rate".
# California combined rates run roughly 7.25% to 10.75%; the band is kept a
# little wider so an unusual district rate still parses.
MIN_TAX_MULTIPLIER = Decimal("1.0")
MAX_TAX_MULTIPLIER = Decimal("1.20")


class ParseError(ValueError):
    pass


@dataclass
class Component:
    """One purchased thing recovered from a single spreadsheet cell."""

    pretax_cents: int
    tax_cents: int
    amount_cents: int
    tax_rate_bp: Optional[int]  # 1025 == 10.25%; None when the cell had no rate
    raw: str

    @property
    def has_tax_detail(self) -> bool:
        return self.tax_rate_bp is not None


def _to_decimal(node: ast.AST) -> Decimal:
    """Evaluate a numeric sub-expression exactly, without ``eval``."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ParseError(f"non-numeric constant: {node.value!r}")
        return Decimal(repr(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _to_decimal(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp):
        left, right = _to_decimal(node.left), _to_decimal(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise ParseError("division by zero")
            return left / right
        raise ParseError(f"unsupported operator: {type(node.op).__name__}")
    raise ParseError(f"unsupported expression node: {type(node).__name__}")


def _split_addends(node: ast.AST) -> list[tuple[ast.AST, int]]:
    """Flatten a + b - c into [(a, +1), (b, +1), (c, -1)]."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
        sign = 1 if isinstance(node.op, ast.Add) else -1
        terms = _split_addends(node.left)
        terms += [(sub, s * sign) for sub, s in _split_addends(node.right)]
        return terms
    return [(node, 1)]


def _component_from_term(node: ast.AST, sign: int, raw: str) -> Component:
    """Read one addend as ``base`` or ``base * (1 + rate)``."""
    pretax: Decimal
    rate: Optional[Decimal] = None

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        right = _to_decimal(node.right)
        left = _to_decimal(node.left)
        if MIN_TAX_MULTIPLIER < right <= MAX_TAX_MULTIPLIER:
            pretax, rate = left, right - 1
        elif MIN_TAX_MULTIPLIER < left <= MAX_TAX_MULTIPLIER:
            # Written the other way round: =1.1025*9.88
            pretax, rate = right, left - 1
        else:
            # A genuine quantity times price, e.g. =3*12.99. No tax detail.
            pretax = left * right
    else:
        pretax = _to_decimal(node)

    pretax *= sign
    gross = pretax * (1 + rate) if rate is not None else pretax

    pretax_cents = to_cents(pretax)
    amount_cents = to_cents(gross)
    return Component(
        pretax_cents=pretax_cents,
        tax_cents=amount_cents - pretax_cents,
        amount_cents=amount_cents,
        tax_rate_bp=int((rate * 10000).to_integral_value()) if rate is not None else None,
        raw=raw,
    )


def parse_amount_cell(cell_value: object) -> list[Component]:
    """Turn one spreadsheet cell into one or more components.

    Accepts a formula string (with or without the leading ``=``) or a plain
    number. Raises ``ParseError`` on anything it cannot read, so the caller can
    fall back to the cached value and flag the row for review.
    """
    if cell_value is None:
        return []
    if isinstance(cell_value, (int, float, Decimal)) and not isinstance(cell_value, bool):
        cents = to_cents(cell_value)
        return [Component(cents, 0, cents, None, str(cell_value))]

    text = str(cell_value).strip()
    if not text:
        return []
    expr = text[1:] if text.startswith("=") else text
    expr = expr.replace("$", "").replace(",", "").strip()
    if not expr:
        return []

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:  # cell references, ranges, functions
        raise ParseError(f"cannot parse {text!r}") from exc

    terms = _split_addends(tree.body)
    return [_component_from_term(node, sign, text) for node, sign in terms]
