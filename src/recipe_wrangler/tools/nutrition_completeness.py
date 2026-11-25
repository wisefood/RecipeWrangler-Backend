"""Nutrient-specific completeness: unknown stays distinct from zero unless the gap is negligible by mass."""
from __future__ import annotations

import math
import re

METRICS=('energy_kcal','protein_g','carbs_g','fat_g','sugar_g','saturated_fat_g','sodium_mg','fibre_g')
REGIONAL={
    'energy_kcal':('Energy (kcal) (kcal)','Energy\nkcal'),
    'protein_g':('Protein (g)','Protein g'),
    'carbs_g':('Carbohydrate (g)','Carbohydrat\nes g','Carbohydrates g'),
    'fat_g':('Fat (g)','Fat g'),'sugar_g':('Total sugars (g)','Sugar (g)'),
    'saturated_fat_g':('Satd FA /100g fd (g)','Saturated Fat (g)'),
    'sodium_mg':('Sodium (mg)','Sodium\nmg','Sodium mg'),
    'fibre_g':('Fibre (g)','Fiber (g)','Fiber /100g fd (g)')}
EU=dict(zip(METRICS,('Energy, kcal','Protein','Carbohydrate, by difference','Total lipid (fat)',
    'Sugars, total','Fatty acids, total saturated','Sodium, Na','Fiber, total dietary')))
# A nutrient total stays usable when lines without a value are at most this share of recipe mass. Chosen from
# HealthyFoods panels: energy bias is unchanged at or below 2% and turns negative above (evaluation/nutrition/README.md).
MISSING_MASS_TOLERANCE=0.02


def finite(value):
    if isinstance(value,bool):return None
    try:result=float(value)
    except (TypeError,ValueError):return None
    return result if math.isfinite(result) and result>=0 else None


def native_values(row: dict | None) -> dict:
    if not row:return dict.fromkeys(METRICS)
    if 'nutrients' in row:
        values={}
        for m,label in EU.items():
            item=row['nutrients'].get(label)
            if m=='sugar_g' and item is None:item=row['nutrients'].get('Sugars, total including NLEA')
            expected='kcal' if m=='energy_kcal' else 'mg' if m=='sodium_mg' else 'g'
            values[m]=finite(item.get('value')) if isinstance(item,dict) and item.get('unit')==expected else None
        if values['energy_kcal'] is None:
            energy=row['nutrients'].get('Energy')
            if isinstance(energy,dict) and energy.get('unit') in {'kcal','kJ'}:
                v=finite(energy.get('value'))
                if v is not None:values['energy_kcal']=v/(4.184 if energy['unit']=='kJ' else 1)
        return values
    values={m:next((finite(row[k]) for k in keys if row.get(k) is not None),None) for m,keys in REGIONAL.items()}
    if values['energy_kcal'] is None:
        kj=next((finite(row[k]) for k in ('Energy (kJ) (kJ)','Energy\nkJ') if row.get(k) is not None),None)
        if kj is not None:values['energy_kcal']=kj/4.184
    return values


def contribution(values: dict, weight_g: float, *, ingredient: str | None = None) -> dict:
    if finite(weight_g) is None:raise ValueError('Ingredient weight must be finite and nonnegative')
    return {'weight_g':weight_g,**({'ingredient':ingredient} if ingredient is not None else {}),
        **{m:0.0 if weight_g==0 else None if values.get(m) is None else values[m]*weight_g/100 for m in METRICS}}


def completeness(contributions: list[dict], serves: float | None) -> dict:
    if serves is not None and (finite(serves) is None or serves<=0):raise ValueError('Servings must be positive and finite')
    missing={m:sum(c.get(m) is None for c in contributions) for m in METRICS}
    partial={m:sum(c[m] for c in contributions if c.get(m) is not None) for m in METRICS}
    mass=sum(c.get('weight_g') or 0 for c in contributions)
    share={m:sum(c.get('weight_g') or 0 for c in contributions if c.get(m) is None)/mass if mass else 0.0 for m in METRICS}
    status={m:'complete' if not missing[m] else 'minor_gaps' if share[m]<=MISSING_MASS_TOLERANCE and mass else 'incomplete' for m in METRICS}
    # A little unknown salt can dominate sodium despite its negligible recipe mass.
    if any(c.get('sodium_mg') is None and re.search(r'\bsalt\b', c.get('ingredient') or '', re.I) for c in contributions):
        status['sodium_mg']='incomplete'
    complete={m:None if status[m]=='incomplete' else partial[m] for m in METRICS}
    gaps=[{'position':c.get('position',i), 'name':c.get('ingredient'), 'weight_g':c.get('weight_g'),
           'missing_nutrients':[m for m in METRICS if c.get(m) is None]}
          for i,c in enumerate(contributions) if any(c.get(m) is None for m in METRICS)]
    return {'complete_totals':complete,'supported_partial_totals':partial,'missing_contributions':missing,'missing_mass_share':share,
        'complete_totals_per_serving':{m:v/serves if v is not None and serves else None for m,v in complete.items()},
        'supported_partial_totals_per_serving':{m:v/serves if serves else None for m,v in partial.items()},
        'nutrient_status':status,**({'ingredient_gaps':gaps} if gaps else {}),
        'nutrition_status':'incomplete' if 'incomplete' in status.values() else 'minor_gaps' if any(missing.values()) else 'complete'}


def apply_totals_mode(result: dict, source: str, mode: str) -> dict:
    """Keep legacy sums available while permitting explicit strict completeness."""
    if mode not in {'legacy','shadow','strict'}:
        raise ValueError('NUTRITION_TOTALS_MODE must be legacy, shadow or strict')
    result['totals_mode']=mode
    if mode=='legacy':return result
    result['legacy_totals']=dict(result['clean_totals'])
    result['legacy_totals_per_serving']=dict(result.get('clean_totals_per_serving') or {})
    if mode=='shadow':return result
    rename=lambda m:'carbohydrate_g' if m=='carbs_g' else m
    result['clean_totals']={rename(m):v for m,v in result['complete_totals'].items()}
    result['clean_totals_per_serving']={rename(m):v for m,v in result['complete_totals_per_serving'].items()}
    for m,value in result['complete_totals'].items():
        result[f'total_{rename(m)}_{source}']=value
        result[f'total_{rename(m)}_per_serving_{source}']=result['complete_totals_per_serving'][m]
    return result
