"""Live cost adaptation of one recipe, with the user's allergens and diet applied.

Same engine as the old cost route (service._generate_cost_suggestions: top cost contributor, same-category cheaper substitute from the graph),
now run from the stored (name, weight) pairs, with no nutrition re-match and no nutri-guard (COST_NUTRITION_GUARDS). The answer has the shape of a
recipe_adaptations row. Without a model the changed line reads `original line  →  use X`; with `llm` (cost_llm: culinary choice, then rewrite of the
line, title and steps) the text is the model's, validated in code. The tip is always one code-written sentence."""
import re

from fastapi import HTTPException

from recipe_wrangler.tools.fetch_recipe_info import fetch_recipe_info_by_id

from . import cost_llm
from . import nutrition_adaptation_llm as L
from . import service
from .nutrition_adaptation import user_restriction_problems

SOURCE = 'cost catalogue reference prices'


def _row(recipe_id, region, status, reason_code=None, **extra):
    return dict(recipe_id=recipe_id, mode='cost', region=region, version='live', source='live', status=status, is_adapted=status == 'adapted',
                reason_code=reason_code, original=None, adapted=None, swap=None, benefit=None, provenance={'source': SOURCE}, **extra)


def _find(lines, name):
    """Index of the recipe line that names the ingredient: the name as a phrase, else all its words."""
    name = name.strip().lower()
    words = [w.rstrip('s') for w in re.findall(r'[a-z]+', name)]
    for test in (lambda t: name in t, lambda t: all(w in t for w in words)):
        for i, t in enumerate(lines):
            if test(t.lower()):
                return i
    return None


def adapt_active_recipe(recipe_id, region, allergens=(), diet=None, llm=None):
    """Read-only. Returns a recipe_adaptations-shaped dict."""
    region = region.upper()
    applied = {'allergens': list(allergens), 'diet': diet}
    try:
        profile = service._load_profile(recipe_id, region)
    except HTTPException:
        return _row(recipe_id, region, 'failed', 'no_active_profile')
    recipe = fetch_recipe_info_by_id(recipe_id)
    ingredients = recipe.get('ingredients') or []
    lines = recipe.get('original_ingredients') or [f"{i.get('measurement') or ''} {i.get('name') or ''}".strip() for i in ingredients]
    own = user_restriction_problems(lines, allergens, diet)
    if own:  # a swap cannot make the recipe safe for this user, so none is offered
        return _row(recipe_id, region, 'not_suitable_for_user', 'recipe_breaks_user_restriction', offending=own, applied_restrictions=applied)
    out = service._generate_cost_suggestions(recipe_id, region, 3 if llm else 1, False, allergens=allergens, diet=diet)
    if not out.get('suggestions'):
        code = out.get('reason_code')
        if (allergens or diet) and code == 'no_cheaper_option' and service._generate_cost_suggestions(recipe_id, region, 1, False).get('suggestions'):
            code = 'alternatives_add_allergen'  # a cheaper swap exists without the restriction, so the restriction is what removed it
        return _row(recipe_id, region, 'no_suggestion', code, applied_restrictions=applied)
    old_name = out['offending_ingredient']
    pos = _find(lines, old_name)
    steps = [L.STEP_NUMBER.sub('', x) for x in L.flat_steps(recipe.get('instructions'))]
    title = recipe.get('title') or recipe_id
    before = round(out['current_cost_per_serving_eur'], 2)
    draft = None
    if llm and pos is not None:
        weight = next((d['weight_g'] for d in profile['nutrition_profiling_details'] if (d.get('name') or d.get('ingredient') or '').strip().lower() == old_name.lower()), None)
        cands = cost_llm.pack(out['suggestions'], old_name, pos, lines[pos], weight, before)
        draft = cost_llm.run(title, [{'position': i, 'text': t, 'weight_g': weight if i == pos else None} for i, t in enumerate(lines)], steps, cands, llm, allergens, diet)
        s = next(x for x in out['suggestions'] if x['rank'] == draft['choice']['id']) if draft['status'] == 'ok' else out['suggestions'][0]
    else:
        s = out['suggestions'][0]
    new_name = s['substitute_name']
    after = round(s['simulated_cost_per_serving_eur'], 2)
    tip = (f"We replace the {old_name} with {new_name} because it costs {round(s['cost_reduction_pct'] * 100)}% less per kg, "
           f"which saves about {before - after:.2f} EUR per serving (reference prices, indicative).")
    if draft and draft['status'] == 'ok':
        adapted = dict(title=draft['title'], lines=[draft['new_lines'].get(i, t) for i, t in enumerate(lines)], steps=draft['steps'], changed_positions=sorted(draft['new_lines']))
        text = 'llm_rewrite: choice and rewrite by the model, validated in code'
    else:
        adapted = dict(title=title, lines=[f'{t}  →  use {new_name}' if i == pos else t for i, t in enumerate(lines)], steps=steps, changed_positions=[] if pos is None else [pos])
        text = 'code_line_only: title and steps are not rewritten'
    row = _row(recipe_id, region, 'adapted', applied_restrictions=applied)
    row.update(
        original=dict(title=title, lines=lines, steps=steps), adapted=adapted,
        swap={'operation': 'SWAP_FOOD', 'source_line': lines[pos] if pos is not None else old_name, 'matched_label': old_name, 'candidate_label': new_name,
              'original_price_eur_kg': s['original_price_eur_kg'], 'candidate_price_eur_kg': s['candidate_price_eur_kg'], 'food_category': s.get('food_category')},
        benefit={'facet': 'cost', 'tip': tip, 'tip_source': 'code',
                 'metric': {'name': 'cost_eur_per_serving', 'unit': 'EUR per serving', 'before': before, 'after': after, 'change': round(after - before, 2)},
                 'swap': [{'from': old_name, 'to': new_name}], 'notes': []},
        provenance={'source': SOURCE, 'text': text, 'flags': {'indicative': True}})
    if draft:
        row['provenance']['llm'] = {k: draft.get(k) for k in ('status', 'choice', 'failure') if draft.get(k) is not None}
    return row
