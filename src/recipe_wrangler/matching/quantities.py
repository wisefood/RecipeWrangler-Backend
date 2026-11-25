"""Resolve recipe amounts before, and independently of, regional food matching."""
from __future__ import annotations

import copy
import math
import re

from recipe_wrangler.matching.evaluation.annotation_experiment import prepare_phrase
from recipe_wrangler.matching.source_evidence import stated_weight,quantity_source_fragment
from recipe_wrangler.repositories.matching_store import digest
from recipe_wrangler.tools.nutrition_completeness import finite, contribution, completeness, METRICS
from recipe_wrangler.utils.seasoning_quantities import seasoning_quantity_assumption

QUANTITY_VERSION='shared_source_quantities_v5_per_serving_scope'


def effective_quantity(quantity: dict, original: str, *, version: str=QUANTITY_VERSION) -> dict:
    """Retain gross source evidence separately from an estimated edible mass."""
    result = copy.deepcopy(quantity)
    effective = adjusted_weight(quantity['weight_g'], original)
    if effective != quantity['weight_g']:
        result.update(weight_g=effective, quantity_evidence=None,
            source_quantity_evidence=copy.deepcopy(quantity.get('quantity_evidence')),
            quantity_status='adjusted_input_quantity_estimate',
            quantity_adjustment={'version': version, 'input_weight_g': quantity['weight_g'],
                'effective_weight_g': effective, 'method': 'source_piece_or_drainage_convention'})
    return result


def valid_quantity_adjustment(detail: dict) -> bool:
    """Verify the recorded convention; its output remains an estimate."""
    try:
        expected = effective_quantity(resolve_quantity(detail['ingredient'], detail['original_weight_g'],
            detail['original_ingredient_text'], detail.get('source_quantity_evidence'),
            serves=(detail.get('per_serving_mass_evidence') or {}).get('serves')),
            detail['original_ingredient_text'], version=detail.get('quantity_version', QUANTITY_VERSION))
        return all(detail.get(key) == expected.get(key) for key in (
            'weight_g', 'quantity_status', 'quantity_evidence', 'source_quantity_evidence', 'quantity_adjustment', 'per_serving_mass_evidence'))
    except (ValueError, TypeError, KeyError):
        return False


def per_person_weight(weight: float, original: str, serves: float) -> float:
    """"60 g rice per person" is a per-serving amount; a weight already scaled stays as it is."""
    stated=re.match(r'^\s*(\d+(?:\.\d+)?)\s*g\b([^()\[\];:\n]*?)\bper (?:person|portion|serving)\b',original,re.I)
    if (not stated or re.search(r'\d|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|few|several)\b',stated[2],re.I)
            or not math.isclose(weight,float(stated[1]),rel_tol=.01)):
        return weight
    if not math.isfinite(serves) or serves<=0:
        raise ValueError('Per-serving amounts require a positive finite serving count')
    return float(stated[1])*serves


# Grams per piece, from recipes that state them: cutlets 33-50 g, shank meat 150-200 g.
_PIECE_GRAMS=((r'\blamb\b.*\bcutlets?\b',50.0),(r'\blamb\b.*\bshanks?\b',175.0))
# A 400 g tin of pulses drains to 240 g on labels.
_DRAINED_LEGUME_SHARE=.6


def adjusted_weight(weight: float, original: str) -> float:
    """Whole-piece counts and drained tins, which a stated or estimated gross weight overstates."""
    text=original.casefold()
    count=re.match(r'^\s*(\d+)\b(?!\s*(?:g|kg|x|×|-))',text)
    if count and not re.search(r'\d\s*(?:g|kg)\b',text):
        for pattern,grams in _PIECE_GRAMS:
            if re.search(pattern,text):return float(count[1])*grams
    if (weight>=150 and re.search(r'\b(?:cans?|tins?|tinned|canned)\b',text) and re.search(r'\b(?:drained|rinsed)\b',text)
            and re.search(r'\b(?:beans?|chick ?peas?|lentils?|garbanzos?)\b',text) and not re.search(r'drained weight|\d\s*g\s+drained|\bor\b',text)):
        return weight*_DRAINED_LEGUME_SHARE
    return weight


def resolve_quantity(name: str, weight: float, original: str='', evidence: dict | None=None, *, serves: float | None=None) -> dict:
    if finite(weight) is None:raise ValueError('Ingredient weight must be finite and nonnegative')
    original_weight = weight
    if serves is not None:
        weight = per_person_weight(weight, original, serves)
    # A deliberately excluded occurrence must remain excluded.
    if weight==0:evidence=None
    elif evidence is None and original:
        # An unquantified serving extra is not another measured ingredient.
        fragment=quantity_source_fragment(name,original)
        identity=re.sub(r',\s*plus extra to serve\s*$','',fragment,flags=re.IGNORECASE)
        evidence=stated_weight(name,fragment,source_name=prepare_phrase(identity))
    if evidence is not None:
        if finite(evidence.get('weight_g')) is None or evidence['weight_g']<=0:
            raise ValueError('Invalid source quantity evidence')
        if evidence.get('kind') not in {'explicit_source_mass','source_volume_estimate'}:
            raise ValueError('Unsupported source quantity evidence')
        # Frozen evidence must still satisfy today's per-item/package safeguards.
        parsed=stated_weight(name,evidence.get('source_line') or '')
        if parsed is None:evidence=None
        elif parsed['kind']!=evidence['kind'] or not math.isclose(parsed['weight_g'],evidence['weight_g'],rel_tol=1e-9):
            raise ValueError('Source quantity evidence disagrees with its source line')
    result = {'original_weight_g':original_weight,'weight_g':evidence['weight_g'] if evidence else weight,
        'quantity_evidence':copy.deepcopy(evidence),
        'quantity_status':evidence['kind'] if evidence else 'excluded_zero_quantity' if weight==0 else 'retained_input_quantity_unverified'}
    assumption = seasoning_quantity_assumption(name, original) if weight > 0 and evidence is None else None
    if assumption:
        result.update(weight_g=assumption['weight_g'], quantity_status='seasoning_policy_estimate',
                      quantity_assumption=assumption)
    if weight != original_weight:
        result['per_serving_mass_evidence'] = {'kind': 'explicit_source_per_serving_mass',
            'source_line': original, 'input_weight_g': original_weight, 'serves': serves,
            'weight_g': weight, 'version': QUANTITY_VERSION}
    return result


def source_recipe_index(recipes: list[dict]) -> dict:
    """Evidence belongs to a recipe occurrence, never to a nutrition region."""
    result={}
    fields=('parsed_name','baseline_parsed_name','saved_weight_g','original_text','quantity_evidence')
    signature=lambda r:[{f:o.get(f) for f in fields} for o in r['occurrences']]
    for recipe in recipes:
        key=recipe['recipe_id']
        if key in result and signature(result[key])!=signature(recipe):
            raise ValueError('Conflicting source evidence for recipe '+key)
        result[key]=recipe
    return result


def recipe_inputs(names: list[str], weights: list[float], originals: list[str],
                  source_recipe: dict | None=None, *, serves: float | None=None) -> dict:
    if not len(names)==len(weights)==len(originals):raise ValueError('Ingredient/quantity/context lengths differ')
    names=list(names);weights=list(weights);originals=list(originals);evidence=[None]*len(names)
    if source_recipe:
        occurrences=source_recipe['occurrences']
        if len(occurrences)!=len(names) or any(
            (o.get('baseline_parsed_name') or o['parsed_name'])!=name or o['saved_weight_g']!=weight
            for o,name,weight in zip(occurrences,names,weights,strict=True)):
            raise ValueError('Source quantity evidence does not align with saved ingredients')
        names=[o['parsed_name'] for o in occurrences]
        originals=[o['original_text'] for o in occurrences]
        evidence=[o.get('quantity_evidence') for o in occurrences]
    quantities=[resolve_quantity(n,w,o,e,serves=serves) for n,w,o,e in zip(names,weights,originals,evidence,strict=True)]
    return {'names':names,'weights':weights,'originals':originals,'quantities':quantities,
        'quantity_input_hash':digest([QUANTITY_VERSION,names,weights,originals,quantities]),
        'quantity_version':QUANTITY_VERSION}


def update_profile_totals(profile: dict, source: str) -> dict:
    contributions=[contribution(d['values_per_100g'],d['weight_g'],ingredient=d.get('ingredient') or d.get('name')) for d in profile['details']]
    for detail,scaled in zip(profile['details'],contributions,strict=True):detail.update(scaled)
    complete=completeness(contributions,profile.get('serves'))
    rename=lambda m:'carbohydrate_g' if m=='carbs_g' else m
    profile.update(complete)
    profile['clean_totals']={rename(m):v for m,v in complete['complete_totals'].items()}
    profile['clean_totals_per_serving']={rename(m):v for m,v in complete['complete_totals_per_serving'].items()}
    for m in METRICS:
        profile[f'total_{rename(m)}_{source}']=complete['complete_totals'][m]
        profile[f'total_{rename(m)}_per_serving_{source}']=complete['complete_totals_per_serving'][m]
    return profile


def repair_profile(profile: dict, inputs: dict, source: str, version: str) -> dict:
    """Rescale stored compositions; preserve all food selections and observed facets."""
    result=copy.deepcopy(profile)
    if not all(len(inputs[key])==len(result['details']) for key in ('names','weights','originals','quantities')):
        raise ValueError('Profile ingredient/quantity/context alignment differs')
    expected=digest([inputs['quantity_version'],inputs['names'],inputs['weights'],inputs['originals'],inputs['quantities']])
    if expected!=inputs['quantity_input_hash']:raise ValueError('Canonical ingredient context hash differs')
    result.pop('matching_run_id',None)
    result['selection_mechanism_version']=result.get('selection_mechanism_version') or result['mechanism_version']
    result['mechanism_version']=version
    result['quantity_input_hash']=inputs['quantity_input_hash']
    result['quantity_version']=QUANTITY_VERSION
    for position,(detail,quantity) in enumerate(zip(result['details'],inputs['quantities'],strict=True)):
        if detail.get('position')!=position:raise ValueError('Profile occurrence positions differ')
        original=inputs['originals'][position]
        if not isinstance(original,str):raise ValueError('Original ingredient text must be a string')
        previous=detail.get('original_ingredient_text') or ''
        if previous and not original:raise ValueError('Cannot discard saved original ingredient context')
        if previous!=original or (detail.get('ingredient') or detail.get('name'))!=inputs['names'][position]:
            detail['source_context_previous']={'ingredient':detail.get('ingredient') or detail.get('name'),
                'original_ingredient_text':previous,'quantity_input_hash':detail.get('quantity_input_hash')}
            # New source text can change observed state/qualifiers and selection.
            # Quantity-only repair must not pretend those facts were re-matched.
            detail['source_context_status']='requires_rematching'
        detail['ingredient']=detail['name']=inputs['names'][position]
        detail['original_ingredient_text']=original
        detail.update(copy.deepcopy(quantity))
        detail['mechanism_version']=version
        detail['quantity_input_hash']=inputs['quantity_input_hash']
        detail['quantity_version']=QUANTITY_VERSION
    result['source_context_rematch_positions']=[d['position'] for d in result['details']
        if d.get('source_context_status')=='requires_rematching']
    return update_profile_totals(result,source)
