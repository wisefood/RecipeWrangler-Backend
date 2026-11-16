"""Conservative value projection of exported measurements, for evaluation only."""
from __future__ import annotations

from fractions import Fraction
import math
import re

VERSION = 'evaluation_measurement_split_v1'
VULGAR = {c: Fraction(n, d) for c, n, d in (
    ('¼', 1, 4), ('½', 1, 2), ('¾', 3, 4), ('⅐', 1, 7), ('⅑', 1, 9), ('⅒', 1, 10),
    ('⅓', 1, 3), ('⅔', 2, 3), ('⅕', 1, 5), ('⅖', 2, 5), ('⅗', 3, 5), ('⅘', 4, 5),
    ('⅙', 1, 6), ('⅚', 5, 6), ('⅛', 1, 8), ('⅜', 3, 8), ('⅝', 5, 8), ('⅞', 7, 8))}
UNIT_GROUPS = {
    'g': 'g gram grams', 'kg': 'kg kilogram kilograms', 'mg': 'mg milligram milligrams',
    'oz': 'oz ounce ounces', 'lb': 'lb lbs pound pounds',
    'ml': 'ml milliliter milliliters millilitre millilitres',
    'l': 'l liter liters litre litres', 'cl': 'cl centiliter centiliters centilitre centilitres',
    'dl': 'dl deciliter deciliters decilitre decilitres',
    'cup': 'cup cups', 'tbsp': 'tbsp tbs tablespoon tablespoons', 'tsp': 'tsp teaspoon teaspoons',
    'pint': 'pint pints pt', 'quart': 'quart quarts qt', 'gallon': 'gallon gallons gal',
    'can': 'can cans', 'tin': 'tin tins', 'bottle': 'bottle bottles', 'jar': 'jar jars',
    'bag': 'bag bags', 'packet': 'packet packets', 'package': 'package packages pkg pkgs',
    'box': 'box boxes', 'bunch': 'bunch bunches', 'clove': 'clove cloves',
    'slice': 'slice slices', 'piece': 'piece pieces', 'stick': 'stick sticks',
    'pinch': 'pinch pinches', 'dash': 'dash dashes', 'sprig': 'sprig sprigs',
    'handful': 'handful handfuls', 'drop': 'drop drops',
}
UNITS = {alias: canonical for canonical, aliases in UNIT_GROUPS.items() for alias in aliases.split()}
UNITS.update({'fl oz': 'fl_oz', 'fluid ounce': 'fl_oz', 'fluid ounces': 'fl_oz'})
_FRAC = r'\d+\s*/\s*\d+'
_QUANTITY = rf'(?:\d+\s+{_FRAC}|\d+\s*[{"".join(VULGAR)}]|{_FRAC}|[{"".join(VULGAR)}]|\d+(?:\.\d+)?|\.\d+)'
SIMPLE = re.compile(rf'\s*(?P<quantity>{_QUANTITY})\s*(?P<unit>[A-Za-z][A-Za-z. ]*)?\s*')
UNQUANTIFIED = {'to taste', 'as needed', 'as required', 'optional', 'for garnish'}


def split_measurement(measurement: str) -> dict:
    """Separate unambiguous numeric/unit values without resolving ranges or grams.

    Offsets refer only to the exported measurement string, never the recipe text.
    Unsupported expressions remain visible; a missing quantity never becomes 1.
    """
    if not isinstance(measurement, str):
        raise ValueError('Exported measurement must be a string')
    result = {'raw_measurement': measurement, 'split_version': VERSION,
              'status': 'review_required', 'reason': 'complex_or_unsupported_measurement',
              'quantity': None, 'quantity_text': None, 'unit': None, 'unit_text': None,
              'quantity_span': None, 'unit_span': None, 'span_scope': 'exported_measurement'}
    if not measurement.strip():
        return {**result, 'status': 'missing', 'reason': 'empty_measurement'}
    if measurement.strip().casefold() in UNQUANTIFIED:
        return {**result, 'status': 'unquantified', 'reason': 'explicit_unspecified_amount'}
    if re.match(r'\s*-', measurement):
        return {**result, 'status': 'invalid', 'reason': 'negative_quantity'}
    match = SIMPLE.fullmatch(measurement)
    if match is None:
        return result
    raw_quantity = match['quantity']; raw_unit = (match['unit'] or '').rstrip()
    unit_key = ' '.join(raw_unit.casefold().replace('.', '').split())
    if raw_unit and unit_key not in UNITS:
        return {**result, 'reason': 'unsupported_unit_or_qualifier'}
    try:
        if raw_quantity[-1] in VULGAR:
            whole = raw_quantity[:-1].strip()
            value = Fraction(int(whole) if whole else 0) + VULGAR[raw_quantity[-1]]
        else:
            mixed = re.fullmatch(rf'(\d+)\s+({_FRAC})', raw_quantity)
            if mixed:
                fraction = Fraction(re.sub(r'\s', '', mixed[2]))
                if fraction >= 1:
                    return {**result, 'reason': 'improper_mixed_fraction'}
                value = Fraction(int(mixed[1])) + fraction
            else:
                value = Fraction(re.sub(r'\s', '', raw_quantity))
                if '/' in raw_quantity and len(raw_quantity.split('/')[0].strip()) > 1:
                    return {**result, 'reason': 'ambiguous_compact_fraction'}
        numeric = float(value)
        if not math.isfinite(numeric) or (value > 0 and numeric == 0):
            raise ValueError('Quantity outside finite numeric range')
    except (ValueError, ZeroDivisionError, OverflowError):
        return {**result, 'status': 'invalid', 'reason': 'invalid_numeric_quantity'}
    unit_start = match.start('unit') if raw_unit else None
    return {**result, 'status': 'parsed', 'reason': None,
            'quantity': numeric, 'quantity_text': raw_quantity,
            'unit': UNITS[unit_key] if raw_unit else None, 'unit_text': raw_unit or None,
            'quantity_span': list(match.span('quantity')),
            'unit_span': [unit_start, unit_start + len(raw_unit)] if raw_unit else None}
