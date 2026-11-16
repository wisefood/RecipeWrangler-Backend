"""Rewrite an ingredient measurement for a different serving count.

Round 2 row 10: a recipe serves what its author decided it serves, and a
member cooking for six had no way to ask for six. Scaling is a display
concern -- nothing is re-profiled and nothing is stored -- so this module is
deliberately a pure string in, string out function with no I/O.

Why not reuse ``ingredient_weight_tool._parse_quantity_value``: it answers
"how much is this" and returns a float, which is all a weight lookup needs.
Scaling has to answer "how do I write this back", and "1 1/2 cups" that
returns as "1.5 cups" reads like a spreadsheet rather than a recipe. Parsing
and rendering have to be one round trip, so they live together here.

The guiding rule is that a measurement we cannot confidently parse is
returned untouched. "Salt to taste" does not double, and a wrong quantity is
worse than an unscaled one.
"""

from __future__ import annotations

import re
from fractions import Fraction

__all__ = ["scale_measurement"]


# "1½" and "1 ½" both occur; both mean one and a half.
_UNICODE_FRACTIONS = {
    "½": Fraction(1, 2),
    "⅓": Fraction(1, 3),
    "⅔": Fraction(2, 3),
    "¼": Fraction(1, 4),
    "¾": Fraction(3, 4),
    "⅕": Fraction(1, 5),
    "⅙": Fraction(1, 6),
    "⅛": Fraction(1, 8),
    "⅜": Fraction(3, 8),
    "⅝": Fraction(5, 8),
    "⅞": Fraction(7, 8),
}

# Units written as decimals. Everything else -- cups, spoons, and bare counts
# like "2 cloves" -- is written as a fraction, because "0.75 cups" is not how
# a recipe talks.
_DECIMAL_UNITS = {
    "g", "gram", "grams", "gr",
    "kg", "kilo", "kilos", "kilogram", "kilograms",
    "mg", "milligram", "milligrams",
    "ml", "millilitre", "millilitres", "milliliter", "milliliters",
    "cl", "centilitre", "centilitres",
    "l", "litre", "litres", "liter", "liters",
    "oz", "ounce", "ounces",
    "lb", "lbs", "pound", "pounds",
}

_NUMBER = r"\d+(?:[.,]\d+)?"
# Order matters: a mixed number has to be tried before a bare integer, or
# "1 1/2" parses as "1" and leaves "1/2" stranded in the unit.
_MIXED_RE = re.compile(rf"^\s*(?P<whole>\d+)\s+(?P<num>\d+)\s*/\s*(?P<den>\d+)\b")
_FRACTION_RE = re.compile(r"^\s*(?P<num>\d+)\s*/\s*(?P<den>\d+)\b")
_DECIMAL_RE = re.compile(rf"^\s*(?P<value>{_NUMBER})")
_RANGE_RE = re.compile(
    rf"^\s*(?P<low>{_NUMBER})\s*(?:-|–|—|\bto\b)\s*(?P<high>{_NUMBER})\b"
)
# "2 x 120 g" scales by the count, never by the pack size: you buy four 120 g
# tins, not two 240 g ones.
_MULTIPLIER_RE = re.compile(
    rf"^\s*(?P<count>\d+)\s*[x×]\s*(?P<rest>{_NUMBER}\s*\S.*)$",
    re.IGNORECASE,
)


def _expand_unicode_fractions(text: str) -> str:
    """"1½" -> "1 1/2", "½" -> "1/2"."""
    out: list[str] = []
    for index, char in enumerate(text):
        fraction = _UNICODE_FRACTIONS.get(char)
        if fraction is None:
            out.append(char)
            continue
        if index and text[index - 1].isdigit():
            out.append(" ")
        out.append(f"{fraction.numerator}/{fraction.denominator}")
    return "".join(out)


# Units that read as words and so have to agree with the number in front of
# them: doubling "1/2 cup" gives "1 cup", not "1 cups". Abbreviations (g, ml,
# tbsp) are deliberately absent -- they do not inflect.
_UNIT_PLURALS = {
    "cup": "cups",
    "tablespoon": "tablespoons",
    "teaspoon": "teaspoons",
    "clove": "cloves",
    "slice": "slices",
    "piece": "pieces",
    "sprig": "sprigs",
    "stick": "sticks",
    "stalk": "stalks",
    "head": "heads",
    "leaf": "leaves",
    "can": "cans",
    "jar": "jars",
    "tin": "tins",
    "packet": "packets",
    "sheet": "sheets",
    "fillet": "fillets",
    "rasher": "rashers",
    "handful": "handfuls",
    "pinch": "pinches",
    "bunch": "bunches",
    "drop": "drops",
    "knob": "knobs",
    "punnet": "punnets",
}
_UNIT_SINGULARS = {plural: singular for singular, plural in _UNIT_PLURALS.items()}


def _agree_with_number(value: Fraction, remainder: str) -> str:
    """Make the unit word in ``remainder`` agree with ``value``."""
    match = re.match(r"^(\s*)([A-Za-z]+)", remainder)
    if not match:
        return remainder
    leading, word = match.group(1), match.group(2)
    lowered = word.lower()
    singular = _UNIT_SINGULARS.get(lowered, lowered)
    if singular not in _UNIT_PLURALS:
        return remainder
    # English takes the singular for anything up to one: "3/4 cup", "1 cup",
    # but "1 1/2 cups".
    wanted = singular if value <= 1 else _UNIT_PLURALS[singular]
    if word[:1].isupper():
        wanted = wanted.capitalize()
    return f"{leading}{wanted}{remainder[match.end():]}"


def _leading_unit(remainder: str) -> str:
    match = re.match(r"^\s*([A-Za-z]+)", remainder)
    return match.group(1).lower().rstrip(".") if match else ""


def _format_decimal(value: Fraction) -> str:
    number = float(value)
    # Whole grams are enough above 10; below that a half gram can matter.
    rounded = round(number) if number >= 10 else round(number, 1)
    text = f"{rounded:g}"
    return text


def _format_fraction(value: Fraction) -> str:
    """Render as a mixed number, snapped to eighths the way recipes are written."""
    snapped = Fraction(round(value * 8), 8)
    if snapped == 0:
        # Too small to write as an eighth, but not nothing -- say the small
        # number rather than rounding an ingredient out of the recipe.
        return f"{float(value):.2f}".rstrip("0").rstrip(".") or "0"
    whole = snapped.numerator // snapped.denominator
    remainder = snapped - whole
    if remainder == 0:
        return str(whole)
    fraction_text = f"{remainder.numerator}/{remainder.denominator}"
    return fraction_text if whole == 0 else f"{whole} {fraction_text}"


def _format(value: Fraction, remainder: str) -> str:
    if _leading_unit(remainder) in _DECIMAL_UNITS:
        return _format_decimal(value)
    return _format_fraction(value)


def _parse_leading_quantity(text: str) -> tuple[Fraction, str] | None:
    """(quantity, everything after it), or None when it does not start with one."""
    mixed = _MIXED_RE.match(text)
    if mixed:
        den = int(mixed.group("den"))
        if den:
            value = int(mixed.group("whole")) + Fraction(int(mixed.group("num")), den)
            return value, text[mixed.end():]
        return None

    fraction = _FRACTION_RE.match(text)
    if fraction:
        den = int(fraction.group("den"))
        if den:
            return Fraction(int(fraction.group("num")), den), text[fraction.end():]
        return None

    decimal = _DECIMAL_RE.match(text)
    if decimal:
        raw = decimal.group("value").replace(",", ".")
        return Fraction(raw), text[decimal.end():]
    return None


def scale_measurement(measurement: str, factor: float | Fraction) -> str:
    """Return ``measurement`` written for ``factor`` times as much.

    ``factor`` is new_serves / original_serves. A measurement with no leading
    quantity ("to taste", "a pinch") comes back untouched, as does anything
    that fails to parse.
    """
    text = str(measurement or "").strip()
    if not text:
        return text
    try:
        ratio = Fraction(factor).limit_denominator(1000)
    except (TypeError, ValueError):
        return text
    if ratio <= 0:
        return text
    if ratio == 1:
        return text

    expanded = _expand_unicode_fractions(text)

    multiplier = _MULTIPLIER_RE.match(expanded)
    if multiplier:
        count = Fraction(int(multiplier.group("count"))) * ratio
        rest = multiplier.group("rest")
        return f"{_format_fraction(count)} x {rest.strip()}"

    span = _RANGE_RE.match(expanded)
    if span:
        remainder = expanded[span.end():]
        low = Fraction(span.group("low").replace(",", ".")) * ratio
        high = Fraction(span.group("high").replace(",", ".")) * ratio
        separator = "-" if "-" in span.group(0) else " to "
        return (
            f"{_format(low, remainder)}{separator}{_format(high, remainder)}"
            f"{_agree_with_number(high, remainder)}"
        )

    parsed = _parse_leading_quantity(expanded)
    if parsed is None:
        return text
    value, remainder = parsed
    scaled = value * ratio
    return f"{_format(scaled, remainder)}{_agree_with_number(scaled, remainder)}"
