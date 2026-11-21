"""Food-ID matching with strict constraints and explained ranked approximations."""
from collections import Counter
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PortionFacets(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    size: Literal['small', 'medium', 'large'] | None = None
    preparation: Literal['raw', 'cooked', 'boiled', 'baked', 'fried', 'roasted', 'steamed'] | None = None
    form: Literal['whole', 'halved', 'pieces', 'slivered', 'chopped', 'sliced', 'diced', 'ground', 'mashed', 'shredded', 'powder', 'liquid'] | None = None
    peel: Literal['with_peel', 'without_peel'] | None = None
    part: Literal['whole', 'white', 'yolk', 'fillet', 'leaf', 'bulb', 'clove', 'stalk', 'root'] | None = None
    preservation: Literal['fresh', 'dried', 'frozen', 'canned', 'pickled', 'smoked'] | None = None
    packing: Literal['packed', 'sifted', 'heaped', 'level'] | None = None


class PortionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    food_id: str | None = None
    facets: PortionFacets = Field(default_factory=PortionFacets)
    region: str | None = None
    match_mode: Literal['ranked', 'strict'] = 'ranked'


class PortionRecord(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    record_id: str
    food_id: str
    source_food_id: str
    food_name: str
    unit: str
    grams_per_unit: float = Field(gt=0, allow_inf_nan=False)
    facets: PortionFacets
    source: str
    source_url: str | None = None
    terms_url: str | None = None
    region: str
    description: str
    original: dict
    excluded_parts: tuple[str, ...] = ()
    unit_volume_ml: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    volume_profile: str | None = None
    blockers: tuple[str, ...] = ()


def infer_facets(text: str) -> tuple[PortionFacets, list[str]]:
    """Extract only explicit text evidence; ambiguity becomes a blocker."""
    text = text.lower()
    # "whole wheat", "whole grain", "whole milk" describe the food, not a whole-item form.
    form_text = re.sub(r'\bwhole[- ]?(?:wheat|grain|meal|milk|egg|eggs)\b', ' ', text)
    words = set(re.findall(r'[a-z]+', text))
    evidence = {
        'size': words & {'small', 'medium', 'large'},
        'preparation': words & {'raw', 'cooked', 'boiled', 'baked', 'fried', 'roasted', 'steamed'},
        'form': set(re.findall(r'[a-z]+', form_text)) & {'whole', 'halved', 'pieces', 'slivered', 'chopped', 'sliced', 'diced', 'ground', 'mashed', 'shredded', 'powder', 'liquid'},
        'part': words & {'fillet', 'leaf', 'bulb', 'clove', 'stalk', 'root'},
        'preservation': words & {'fresh', 'dried', 'frozen', 'canned', 'pickled', 'smoked'},
        'packing': words & {'packed', 'sifted', 'heaped', 'level'},
        'peel': set(),
    }
    for word, form in {'slices': 'sliced', 'slice': 'sliced', 'halves': 'halved', 'chips': 'pieces', 'powdered': 'powder', 'pureed': 'mashed'}.items():
        if word in words: evidence['form'].add(form)
    for plural, singular in {'fillets': 'fillet', 'leaves': 'leaf', 'bulbs': 'bulb', 'cloves': 'clove', 'stalks': 'stalk', 'roots': 'root'}.items():
        if plural in words: evidence['part'].add(singular)
    if words & {'dry', 'dehydrated'}: evidence['preservation'].add('dried')
    # A specific cooking method includes the broad cooked state.
    if len(evidence['preparation']) > 1 and 'raw' not in evidence['preparation']:
        evidence['preparation'].discard('cooked')
    if 'peeled' in words or re.search(r'\b(?:without|no) (?:skin|peel)\b', text):
        evidence['peel'].add('without_peel')
    if 'unpeeled' in words or re.search(r'\bwith (?:skin|peel)\b|flesh and skin', text):
        evidence['peel'].add('with_peel')
    if words & {'egg', 'eggs'}:
        for part in ('white', 'yolk', 'whole'):
            if part in words or part + 's' in words: evidence['part'].add(part)
    blockers = [f'ambiguous_{key}' for key, values in evidence.items() if len(values) > 1]
    if 'or' in words and words & {'quartered', 'halved', 'chunks', 'cubes'} and evidence['form']:
        blockers.append('ambiguous_form')
    if re.search(r'\bnot\s+(?:packed|sifted|heaped|level)\b', text):
        blockers.append('unsupported_packing_negation')
    if re.search(r'\bwith (?:core|pit|seed)s?\b', text):
        blockers.append('unsupported_edible_basis')
    values = {key: next(iter(values)) for key, values in evidence.items() if len(values) == 1}
    return PortionFacets(**values), blockers


def merge_facets(observed: PortionFacets, supplied: PortionFacets) -> tuple[PortionFacets, list[str]]:
    values = observed.model_dump(exclude_none=True)
    blockers = []
    for key, value in supplied.model_dump(exclude_none=True).items():
        if key in values and values[key] != value:
            blockers.append(f'conflicting_input_{key}')
        values[key] = value
    return PortionFacets(**values), blockers


def select_portion(records: list[PortionRecord], food_id: str, unit: str,
                   facets: PortionFacets, region: str | None = None,
                   unit_volumes: dict | None = None) -> dict:
    query_volume = {'ml': 1, 'dl': 100, 'l': 1000}.get(unit) or (unit_volumes or {}).get(unit)
    rejected = Counter()
    accepted = []
    wanted = facets.model_dump(exclude_none=True)
    for record in records:
        reason = None
        if record.food_id != food_id: reason = 'food_identity_mismatch'
        elif record.unit != unit and not (query_volume and record.unit_volume_ml): reason = 'unit_mismatch'
        elif record.blockers: reason = 'ambiguous_source_facets'
        else:
            for key, value in wanted.items():
                actual = getattr(record.facets, key)
                if key == 'preparation' and value == 'cooked' and actual in {'boiled', 'baked', 'fried', 'roasted', 'steamed'}:
                    continue
                if actual != value:
                    reason = f'{key}_unknown' if actual is None else f'{key}_mismatch'
                    break
        if reason:
            rejected[reason] += 1
        else:
            accepted.append(record)
    if not accepted:
        return {'status': 'unresolved', 'record': None, 'rejections': dict(rejected)}
    # Region is a preference after compatibility, never a substitute for it.
    def rank(row):
        # Keep the established source on a verified identity; new countries add
        # coverage without silently replacing existing national defaults.
        source_rank = 0 if row.source_food_id.startswith(('usda_weights:', 'fndds_weights:')) else 1
        if region:
            if row.region.lower() == region.lower(): return (0, source_rank)
            return (1 if row.region in {'NL', 'EU'} else 2, source_rank)
        return (0 if row.region in {'NL', 'EU'} else 1, source_rank)
    best_rank = min(map(rank, accepted))
    best = [r for r in accepted if rank(r) == best_rank]
    if any(r.unit == unit for r in best):
        best = [r for r in best if r.unit == unit]
    for key, defaults in [('form', {None, 'whole'}), ('packing', {None}), ('size', {None})]:
        if key not in wanted and any(getattr(r.facets, key) in defaults for r in best):
            best = [r for r in best if getattr(r.facets, key) in defaults]
    if 'form' not in wanted:
        form_rank = {'whole': 0, None: 1, 'halved': 2, 'pieces': 3,
                     'chopped': 4, 'sliced': 4, 'diced': 4, 'slivered': 4,
                     'shredded': 4, 'ground': 5, 'mashed': 5, 'powder': 6, 'liquid': 0}
        preferred_form = min(form_rank[r.facets.form] for r in best)
        best = [r for r in best if form_rank[r.facets.form] == preferred_form]
    # For metric-volume conversion, a larger same-form measure reduces rounding.
    if query_volume and not any(r.unit == unit for r in best):
        facet_sets = {tuple(r.facets.model_dump().items()) for r in best}
        if len(facet_sets) == 1:
            densities = [r.grams_per_unit / r.unit_volume_ml for r in best]
            if max(densities) / min(densities) <= 1.01:
                largest = max(r.unit_volume_ml for r in best)
                best = [r for r in best if r.unit_volume_ml == largest]
    # Do not arbitrarily choose between chopped/whole, sized/unsized or different weights.
    signatures = {(r.grams_per_unit, tuple(r.facets.model_dump().items())) for r in best}
    # Fractional reported amounts (e.g. 0.33 cup) magnify gram rounding.
    # Resolve only same-source, same-unit, same-facet near-duplicates, using
    # the largest original measured amount rather than averaging the grams.
    if (len(signatures) > 1 and len({r.source_food_id for r in best}) == 1
            and len({r.unit for r in best}) == 1
            and len({tuple(r.facets.model_dump().items()) for r in best}) == 1
            and any(0 < float(r.original.get('amount') or 1) < 1 for r in best)
            and max(r.grams_per_unit for r in best) / min(r.grams_per_unit for r in best) <= 1.02):
        best = [min(best, key=lambda r: (-float(r.original.get('amount') or 1), r.record_id))]
        signatures = {(best[0].grams_per_unit, tuple(best[0].facets.model_dump().items()))}
    if len(signatures) > 1:
        return {'status': 'ambiguous', 'record': None, 'rejections': dict(rejected),
                'candidate_ids': sorted(r.record_id for r in best)}
    chosen = min(best, key=lambda r: r.record_id)
    assert math.isfinite(chosen.grams_per_unit)
    record = chosen.model_dump()
    if chosen.unit != unit or (query_volume and chosen.unit_volume_ml and query_volume != chosen.unit_volume_ml):
        volume = query_volume
        record['reference_grams_per_unit'] = chosen.grams_per_unit
        record['reference_unit'] = chosen.unit
        record['unit'] = unit
        record['grams_per_unit'] = chosen.grams_per_unit * volume / chosen.unit_volume_ml
        record['unit_conversion'] = {'requested_unit': unit, 'reference_unit': chosen.unit,
                                     'reference_volume_ml': chosen.unit_volume_ml,
                                     'profile': chosen.volume_profile}
    return {'status': 'matched', 'record': record, 'rejections': dict(rejected)}


def rank_portions(records: list[PortionRecord], food_id: str, unit: str,
                  facets: PortionFacets, region: str | None = None,
                  unit_volumes: dict | None = None) -> dict:
    """Prefer exact compatibility; rank limited approximations without changing source grams.

    Scores are a deterministic compatibility rubric, not accuracy probabilities.
    Unknown size: regular→medium costs0.15, other unsized whole counts0.25;
    adjacent explicit size costs0.30. Unknown preparation/preservation costs0.10,
    and unknown packing0.15. Contradictions outside size are never relaxed.
    """
    exact = select_portion(records, food_id, unit, facets, region, unit_volumes)
    if exact['record'] is not None:
        return dict(exact, match_quality='exact', match_score=1.0,
                    score_kind='heuristic_compatibility', approximations=[])
    # Equal-priority, fully compatible conflicting records require adjudication.
    if exact['status'] == 'ambiguous':
        # Small spoons amplify rounded source grams and national volume-profile
        # differences. Prefer a larger reported reference, with an explicit
        # uncertainty flag; larger disagreements still require adjudication.
        ambiguous = [r for r in records if r.record_id in exact.get('candidate_ids', [])]
        if (ambiguous and all(r.unit_volume_ml for r in ambiguous)
                and len({r.source_food_id for r in ambiguous}) == 1
                and len({tuple(r.facets.model_dump().items()) for r in ambiguous}) == 1):
            densities = [r.grams_per_unit / r.unit_volume_ml for r in ambiguous]
            spread = max(densities) / min(densities)
            if spread <= 1.10:
                reference = min(ambiguous, key=lambda r: (-r.unit_volume_ml, r.record_id))
                selected = select_portion([reference], food_id, unit, facets, region, unit_volumes)
                if selected['record'] is not None:
                    return dict(selected, match_quality='approximate', match_score=0.90,
                                score_kind='heuristic_compatibility', approximations=[{
                                    'facet':'volume_reference', 'reason':'source_rounding_or_volume_profile_disagreement',
                                    'relative_density_spread':spread - 1, 'score_penalty':0.10}])
        # Same food, unit and facets, differing only by an unstated variant (pasta shape):
        # take the median source record rather than abstain or average.
        cut_forms = {None, 'pieces', 'chopped', 'sliced', 'diced', 'shredded', 'slivered'}
        without_form = lambda r: tuple((k, v) for k, v in r.facets.model_dump().items() if k != 'form')
        same_except_cut = (len({without_form(r) for r in ambiguous}) == 1
                           and all(r.facets.form in cut_forms for r in ambiguous) and 'form' not in facets.model_dump(exclude_none=True))
        if ((len(ambiguous) >= 3 or same_except_cut) and len(ambiguous) >= 2
                and len({r.source_food_id for r in ambiguous}) == 1
                and len({r.unit for r in ambiguous}) == 1
                and (len({tuple(r.facets.model_dump().items()) for r in ambiguous}) == 1 or same_except_cut)):
            ordered = sorted(ambiguous, key=lambda r: (r.grams_per_unit, r.record_id))
            reference = ordered[(len(ordered) - 1) // 2]  # lower median, an actual source record
            selected = select_portion([reference], food_id, unit, facets, region, unit_volumes)
            if selected['record'] is not None:
                return dict(selected, match_quality='approximate', match_score=0.80,
                            score_kind='heuristic_compatibility', approximations=[{
                                'facet': 'variant', 'reason': 'unspecified_variant_median',
                                'variants': len(ambiguous), 'score_penalty': 0.20}])
        return dict(exact, match_quality='unresolved', match_score=0.0,
                    score_kind='heuristic_compatibility', approximations=[])
    wanted = facets.model_dump(exclude_none=True)
    candidates = []
    sizes = {'small': 0, 'medium': 1, 'large': 2}
    for row in records:
        if row.food_id != food_id or row.blockers:
            continue
        relaxed = dict(wanted)
        approximations = []
        penalty = 0.0
        allowed = True
        for key, value in wanted.items():
            actual = getattr(row.facets, key)
            if actual == value or (key == 'preparation' and value == 'cooked'
                                  and actual in {'boiled', 'baked', 'fried', 'roasted', 'steamed'}):
                continue
            cost = None
            reason = None
            if key == 'size' and unit in {'piece', 'head', 'egg', 'nut', 'fillet', 'leaf'} and row.unit == unit and row.facets.form == 'whole' and (row.facets.part in {None, 'whole', 'bulb'} or row.facets.part == facets.part):
                if (actual is None and re.search(r'\b(?:pieces?|bite|slices?|wedges?|chunks?)\b', row.description.lower())
                        and 'whole' not in row.description.lower()
                        # ponytail: USDA meat/fish 'piece' is the whole cut (breast 272 g); extend list if new cuts appear
                        and not set(re.findall(r'[a-z]+', row.food_name.lower())) & {'chicken', 'beef', 'pork', 'lamb', 'turkey', 'veal', 'fish', 'salmon', 'cod', 'tuna'}):
                    pass  # generic source "piece" is a cut, not a sized whole item
                elif actual is None:
                    regular = bool(re.match(r'^regular\b', row.description.lower()))
                    cost = 0.15 if regular and value == 'medium' else 0.25
                    reason = 'regular_whole_item_used_for_medium' if regular and value == 'medium' else 'unsized_whole_item_proxy'
                elif abs(sizes[actual] - sizes[value]) == 1:
                    cost = 0.30
                    reason = 'adjacent_size_proxy'
            elif key == 'part' and value == 'leaf' and actual is None and row.unit != 'piece':
                cost = 0.10
                reason = 'herb_volume_is_leaf_basis'  # "mint leaves, 1 cup" vs a herb cup measure
            elif key == 'form' and value == 'ground' and actual in {None, 'whole'} and row.food_name.lower().startswith('spices,'):
                cost = 0.10
                reason = 'spice_measure_basis'
            elif (key == 'size' and unit == 'piece' and row.unit == unit and actual and value
                  and abs(sizes[actual] - sizes[value]) == 1 and facets.part in {'white', 'yolk'} and row.facets.part == facets.part):
                cost = 0.30
                reason = 'adjacent_size_proxy'
            elif (key == 'peel' and value == 'without_peel' and actual is None and row.source_food_id.startswith(('usda_weights:', 'fndds_weights:'))
                  and unit not in {'piece', 'head', 'egg', 'nut', 'fillet', 'leaf'}):
                cost = 0.15
                reason = 'usda_edible_portion_basis'
            elif (key == 'preservation' and value == 'dried' and actual is None and row.facets.preparation == 'raw'
                  and set(re.findall(r'[a-z]+', row.food_name.lower())) & {'lentils', 'soybeans', 'chickpeas', 'beans', 'rice', 'barley', 'oats'}):
                cost = 0.10
                reason = 'dry_staple_raw_is_dried'
            elif actual is None and key in {'preparation', 'preservation', 'packing'} and not (key == 'preservation' and value != 'fresh'):
                cost = 0.15 if key == 'packing' else 0.10
                reason = 'source_facet_not_stated'
            if cost is None:
                allowed = False
                break
            relaxed.pop(key)
            penalty += cost
            approximations.append({'facet': key, 'requested': value, 'reference': actual,
                                   'reason': reason, 'score_penalty': cost})
        score = round(1.0 - penalty, 4)
        if not allowed or not approximations or score < 0.65:
            continue
        candidate = select_portion([row], food_id, unit, PortionFacets(**relaxed), region, unit_volumes)
        if candidate['record'] is not None:
            candidates.append((score, row, candidate['record'], approximations))
    def regional_rank(item):
        row = item[1]
        regional = 0 if region and row.region.lower() == region.lower() else 1 if row.region in {'NL', 'EU'} else 2
        native = 0 if row.source_food_id.startswith(('usda_weights:', 'fndds_weights:')) else 1
        return (regional, native, row.unit != unit)
    candidates.sort(key=lambda item: (-item[0], regional_rank(item), item[1].record_id))
    alternatives = [{'record_id': row.record_id, 'source_food_id': row.source_food_id,
                     'portion_desc': row.description, 'grams_per_unit': record['grams_per_unit'],
                     'unit': record['unit'], 'match_score': score}
                    for score, row, record, _ in candidates[:3]]
    if not candidates:
        return dict(exact, match_quality='unresolved', match_score=0.0,
                    score_kind='heuristic_compatibility', approximations=[], alternatives=[])
    score = candidates[0][0]
    best = [item for item in candidates if item[0] == score]
    preferred = min(map(regional_rank, best))
    best = [item for item in best if regional_rank(item) == preferred]
    if 'form' not in wanted:
        form_rank = {'whole': 0, None: 1, 'halved': 2, 'pieces': 3, 'chopped': 4, 'sliced': 4, 'diced': 4,
                     'slivered': 4, 'shredded': 4, 'ground': 5, 'mashed': 5, 'powder': 6, 'liquid': 0}
        lowest = min(form_rank[item[1].facets.form] for item in best)
        best = [item for item in best if form_rank[item[1].facets.form] == lowest]
    signatures = {(item[2]['grams_per_unit'], tuple(item[1].facets.model_dump().items())) for item in best}
    density_values = [item[2]['grams_per_unit'] for item in best]
    equivalent_density = (len({tuple(item[1].facets.model_dump().items()) for item in best}) == 1
                          and all(item[1].unit_volume_ml for item in best)
                          and max(density_values) / min(density_values) <= 1.01)
    if len(signatures) > 1 and equivalent_density:
        best.sort(key=lambda item: (-item[1].unit_volume_ml, item[1].record_id))
        best = best[:1]
        signatures = {next(iter(signatures))}
    grams = sorted(item[2]['grams_per_unit'] for item in best)
    if (len(signatures) > 1 and len({item[1].source_food_id for item in best}) == 1
            and len({tuple((k, v) for k, v in item[1].facets.model_dump().items() if k != 'peel') for item in best}) == 1
            and grams[-1] / grams[0] <= 1.10):
        # Same source, same facets, within 10% (drumstick 122 vs 130 g): lower median, not abstention.
        best = [sorted(best, key=lambda item: (item[2]['grams_per_unit'], item[1].record_id))[(len(best) - 1) // 2]]
        signatures = {next(iter(signatures))}
    if len(signatures) > 1:
        return {'status': 'ambiguous', 'record': None, 'match_quality': 'unresolved',
                'match_score': score, 'score_kind': 'heuristic_compatibility',
                'approximations': [], 'alternatives': alternatives, 'rejections': exact['rejections']}
    _, _, record, approximations = best[0]
    return {'status': 'matched', 'record': record, 'match_quality': 'approximate',
            'match_score': score, 'score_kind': 'heuristic_compatibility',
            'approximations': approximations, 'alternatives': alternatives,
            'rejections': exact['rejections']}
