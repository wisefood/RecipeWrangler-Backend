"""Conservative recovery of explicit total mass from count-based source lines."""
from __future__ import annotations
import re


def explicit_total_mass(line: str) -> float | None:
    """Recover '4 potatoes (160g / 5oz)' or '1 potato / 130g'.

    Per-item/package, alternative, drained and range quantities need separate
    interpretation; this rule deliberately returns unknown for those cases.
    """
    text=line.casefold().strip()
    if re.search(r'\b(each|per|pack|packs|packet|packets|can|cans|tin|tins|or|drained)\b|\d\s*[-–]\s*\d',text):
        return None
    match=re.match(r'^\d+(?:\.\d+)?\s+[a-z][a-z\s-]*?(?:/|\()\s*(\d+(?:\.\d+)?)\s*(kg|g|grams?)\b',text)
    if not match:
        return None
    mass=float(match[1])*(1000 if match[2]=='kg' else 1)
    # A second metric amount might be a package, serving or edible fraction.
    if len(re.findall(r'\d+(?:\.\d+)?\s*(?:kg|g|grams?)\b',text))!=1:
        return None
    return mass if 0 < mass <= 10000 else None


def name_tokens(text: str) -> set[str]:
    words=re.findall(r'[a-z]+',text.casefold())
    return {w[:-2] if w.endswith('oes') else w[:-1] if w.endswith('s') and len(w)>3 else w for w in words}


def recover_recipe_quantities(recipe: dict) -> list[dict]:
    """Return an evidence ledger; mutate no recipe or source record."""
    changes=[]
    occurrences=recipe['occurrences']
    for line in recipe.get('original_ingredients') or []:
        mass=explicit_total_mass(line)
        if mass is None:
            continue
        words=name_tokens(line)
        matches=[o for o in occurrences if name_tokens(o['parsed_name']) and name_tokens(o['parsed_name'])<=words]
        if len(matches)!=1:
            continue
        occurrence=matches[0]
        # The same ingredient appearing on several source lines is not a unique join.
        if sum(name_tokens(occurrence['parsed_name'])<=name_tokens(other) for other in recipe['original_ingredients'])!=1:
            continue
        if abs(mass-occurrence['saved_weight_g']) > 1e-8:
            changes.append({'recipe_id':recipe['recipe_id'],'occurrence_id':occurrence['occurrence_id'],
                'input':occurrence['parsed_name'],'source_line':line,'old_weight_g':occurrence['saved_weight_g'],
                'new_weight_g':mass,'reason':'explicit_source_total_mass'})
    return changes
