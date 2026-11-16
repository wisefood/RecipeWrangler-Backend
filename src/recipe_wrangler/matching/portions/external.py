"""Source-preserving external portion adapters; no Fineli or fuzzy crosswalks."""
from collections import defaultdict
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re

from .core import PortionRecord, PortionFacets, infer_facets
from .sources import identity_key, COUNT_UNITS, UNSUPPORTED_SCALE_RE
from recipe_wrangler.utils import weigh_calculation_usda_ as usda

DEFAULT_EXTERNAL = Path(__file__).resolve().parents[4] / 'data/processed/weight_reference/structured_external_portions.jsonl'


# Reviewed same-food links where full labels differ (brand/cultivar wording).
REVIEWED_CROSSWALK = {'nz_concise2021:L1267': 'usda_weights:09148'}  # green kiwifruit


def external_records(ausnut, nz, foundation, native_rows):
    """Import measured portions/densities with exact full-label crosswalks only."""
    names = defaultdict(set)
    for row in native_rows:
        native = identity_key(str(row['usda_id'])) if row.get('usda_id') else 'fndds_weights:' + str(row['food_code'])
        names[tuple(re.findall(r'[a-z0-9]+', row['food_name'].casefold()))].add(native)
    def food_key(label, native):
        if native in REVIEWED_CROSSWALK: return REVIEWED_CROSSWALK[native]
        ids = names.get(tuple(re.findall(r'[a-z0-9]+', label.casefold())), set())
        primary = {x for x in ids if x.startswith(('ingredient:', 'usda_weights:'))}
        return next(iter(primary)) if len(primary) == 1 else next(iter(ids)) if len(ids) == 1 else native
    for collection in (ausnut, nz, foundation):
        for row in collection:
            label = row['food_name']; volume = None; blockers = []
            if 'source_measure_id' in row:
                native = 'ausnut2023:' + row['source_food_id']; measure = str(row['source_measure_id'])
                desc = ' '.join(str(x) for x in row['descriptors'] if x is not None)
                density = row['reference_kind'] == 'density_g_per_ml'
                amount = 1 if density else row['quantity']
                grams = row['source_gram_amount_field']
                if density:
                    unit = 'ml'; volume = 1
                else:
                    unit = usda._portion_unit(desc)
                    if row['source_volume_field'] and row['source_volume_field'] > 0 and amount:
                        volume = row['source_volume_field'] / amount
                source = 'AUSNUT 2023, Food Standards Australia New Zealand'
                url = 'https://www.foodstandards.gov.au/science-data/food-nutrient-databases/ausnut/food-measures'
                terms = 'https://www.foodstandards.gov.au/legal-policies/copyright'
                region = 'AU'
            elif 'portion_description' in row:
                native = 'nz_concise2021:' + row['source_food_id']
                desc = row['portion_description']; measure = hashlib.sha256(desc.encode()).hexdigest()[:16]
                match = re.match(r'^(\d+(?:\.\d+)?)\s+(.+)', desc)
                if not match: continue
                amount = float(match[1]); grams = row['grams']; unit = usda._portion_unit(match[2])
                ml = re.search(r'(\d+(?:\.\d+)?)\s*m[lL]', desc)
                if ml: volume = float(ml[1]) / amount
                source = 'The Concise New Zealand Food Composition Tables, 14th Edition 2021, NZ Food Composition Database (2022)'
                url = 'https://www.foodcomposition.co.nz/foodfiles/concise-tables/'
                terms = 'https://www.foodcomposition.co.nz/terms/'
                region = 'NZ'
            else:
                native = 'fdc_foundation:' + str(row['fdc_id']); p = row['portion']
                measure = str(p['id']); amount = p.get('amount'); grams = p.get('gramWeight')
                unit = p.get('measureUnit', {}).get('name', '').lower()
                if unit in {'undetermined', ''}: unit = usda._portion_unit(p.get('modifier', ''))
                desc = (p.get('modifier') or unit).strip()
                source = 'USDA FoodData Central Foundation, October 2026'
                url = 'https://fdc.nal.usda.gov/download-datasets/'
                terms = 'https://fdc.nal.usda.gov/data-documentation.html'
                region = 'US'
            if UNSUPPORTED_SCALE_RE.search(desc): flags_extra = ['unsupported_size_scale']
            else: flags_extra = []
            try:
                if float(amount) <= 0 or float(grams) <= 0: continue
                per = float(grams) / float(amount)
                if not math.isfinite(per) or per <= 0: continue
            except (TypeError, ValueError, ZeroDivisionError): continue
            unit = usda._norm_unit(unit)
            if unit in {'g', 'gram', 'ounce', 'oz', 'pound', 'serving', 'racc', '', 'guideline', 'portion'}:
                continue
            if usda._is_ambiguous_portion_desc(desc): continue
            unit = COUNT_UNITS.get(unit, unit)
            label_words = {usda._normalize_token(w) for w in re.findall(r'[a-z]+', label.lower())}
            if (usda._normalize_token(unit) in label_words
                    or usda._normalize_token(unit) == usda._normalize_token(re.sub(r'[^a-z]', '', label.split(',')[0].lower()))):
                unit = 'piece'  # AUSNUT "parsnip" on Parsnip = one whole parsnip
            if unit in {'small', 'medium', 'large'}: unit = 'piece'
            facets, flags = infer_facets(label + ', ' + ('' if desc == 'density' else desc))
            size, _ = infer_facets(desc)
            values = facets.model_dump(); values['size'] = size.size
            if unit in {'piece', 'nut', 'egg', 'head', 'fillet', 'leaf'} and values['form'] is None: values['form'] = 'whole'
            # A density row is a g/mL reference, not a new food preparation.
            if 'source_measure_id' in row and row['reference_kind'] == 'density_g_per_ml': unit = 'ml'
            original = dict(row)
            # NZ "1 fruit (113 g)" on a flesh-only food: bracket = whole fruit as purchased, grams = flesh.
            whole = re.search(r'\((\d+(?:\.\d+)?)\s*g\)', desc) if 'flesh' in label.lower() else None
            if whole and unit == 'piece':
                values['peel'] = 'without_peel'
                try:
                    yield PortionRecord(record_id=native+':'+measure+':as_purchased', food_id=food_key(label, native),
                                        source_food_id=native, food_name=label, unit=unit,
                                        grams_per_unit=float(whole[1]) / float(amount),
                                        facets=PortionFacets(**dict(values, peel='with_peel')), source=source, region=region,
                                        source_url=url, terms_url=terms, description=desc + ' (whole fruit, as purchased)',
                                        original=original, blockers=tuple(sorted(set(flags+blockers))))
                except ValueError: pass
            try:
                yield PortionRecord(record_id=native+':'+measure, food_id=food_key(label, native), source_food_id=native,
                                    food_name=label, unit=unit, grams_per_unit=per,
                                    facets=PortionFacets(**values), source=source, region=region,
                                    source_url=url, terms_url=terms,
                                    description=desc, original=original, blockers=tuple(sorted(set(flags+blockers+flags_extra))),
                                    unit_volume_ml=volume, volume_profile='source_reported_measure_volume' if volume else None)
            except ValueError: continue


@lru_cache(maxsize=1)
def external_index():
    path = Path(os.getenv('WEIGHT_EXTERNAL_PORTIONS_PATH', str(DEFAULT_EXTERNAL)))
    by_food = defaultdict(list); by_native = defaultdict(list)
    if path.exists():
        with path.open() as stream:
            for line in stream:
                record = PortionRecord.model_validate_json(line)
                by_food[record.food_id].append(record); by_native[record.source_food_id].append(record)
    return by_food, by_native


def external_for(food_id):
    by_food, by_native = external_index()
    return by_food.get(food_id) or by_native.get(food_id, [])


def external_identity(native):
    records = external_index()[1].get(native, [])
    ids = {r.food_id for r in records}
    return next(iter(ids)) if len(ids) == 1 else None
