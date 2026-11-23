"""Live time adaptation of one recipe: start a later, independent step while a passive step (simmer, boil, bake, rest) runs.

Code only, no model: deterministic step understanding, overlap discovery, then a start-only rewrite of the work step. The overlap is inferred by
code and not reviewed by a person, so the row says so (`provenance.flags`) and claims no minutes; the bounded timing, when known, is in
`provenance.timing`. Allergens and diet cannot change the result (no ingredient changes) but a recipe that already breaks them is not offered."""
import re

from recipe_wrangler.tools.fetch_recipe_info import fetch_recipe_info_by_id

from . import nutrition_adaptation_llm as L
from .time_llm import estimate_minutes
from .nutrition_adaptation import user_restriction_problems
from .time_planning import discover_overlaps
from .time_understanding import PASSIVE_ANCHOR, _stage_name, deterministic_understanding

SOURCE = 'recipe instruction text'
# ponytail: word list from the 48 Living Lab candidates of 23 Nov; a step that is itself a wait, a serving/storing/cooling step or a note is not work to move
NOT_WORK = re.compile(r'\b(?:serv\w*|store|storing|fridge|refrigerat\w*|freez\w*|frozen|defrost\w*|cool\w*|chill\w*|garnish\w*|enjoy|leftovers?|keep|decorat\w*|drain\w*|read more|watch|video|tip|per (?:the )?(?:pack|packet|package) instructions?|as per)\b', re.I)

PREP = re.compile(r'^\W*(?:(?:then|next|also|meanwhile)\s+)?(?:peel|chop|dice|slice|cut|trim|mince|grate|rinse|wash|zest|juice|crush|mash|whisk|mix|stir together|combine|prepare|make|measure|sieve|sift|shred|segment|halve|deseed|core)\w*\b', re.I)
# a prep step that points at cooked output, the oven or the pan is not independent of the wait
DEPENDS = re.compile(r'\b(?:cooked|mixture|sauce|soup|oven|preheat\w*|take out|remove|add(?:ed)? (?:to|it|them)|stir in|pour (?:in|over|into)|top with|into the (?:pan|pot|dish))\b', re.I)
MIN_WAIT = 5


def _row(recipe_id, region, status, reason_code=None, **extra):
    return dict(recipe_id=recipe_id, mode='time', region=region, version='live', source='live', status=status, is_adapted=status == 'adapted',
                reason_code=reason_code, original=None, adapted=None, swap=None, benefit=None, provenance={'source': SOURCE}, **extra)


def adapt_active_recipe(recipe_id, region, allergens=(), diet=None, llm=None):
    """Read-only. Returns a recipe_adaptations-shaped dict."""
    region = region.upper()
    applied = {'allergens': list(allergens), 'diet': diet}
    recipe = fetch_recipe_info_by_id(recipe_id)
    lines = recipe.get('original_ingredients') or [f"{i.get('measurement') or ''} {i.get('name') or ''}".strip() for i in recipe.get('ingredients') or []]
    own = user_restriction_problems(lines, allergens, diet)
    if own:
        return _row(recipe_id, region, 'not_suitable_for_user', 'recipe_breaks_user_restriction', offending=own, applied_restrictions=applied)
    steps = [L.STEP_NUMBER.sub('', x) for x in L.flat_steps(recipe.get('instructions'))]
    if not steps:
        return _row(recipe_id, region, 'not_scored', 'no_instructions', applied_restrictions=applied)
    title = recipe.get('title') or recipe_id
    plan = dict(recipe_id=recipe_id, title=title, originals=lines, instructions=steps)
    try:
        understanding = deterministic_understanding(plan)
        found = discover_overlaps(plan, understanding)
    except ValueError:
        return _row(recipe_id, region, 'not_scored', 'recipe_exceeds_planning_bounds', applied_restrictions=applied)
    if found.status == 'needs_evidence':
        return _row(recipe_id, region, 'not_scored', found.reasons[0], applied_restrictions=applied)
    last = len(steps) - 1
    found.candidates = [x for x in found.candidates if (w := int(x.work_step_id.split(':')[1])) < last
                        and PREP.match(steps[w]) and not NOT_WORK.search(steps[w]) and not DEPENDS.search(steps[w])
                        and not PASSIVE_ANCHOR.search(steps[w]) and x.window_minutes.lower >= MIN_WAIT]
    if not found.candidates:
        return _row(recipe_id, region, 'no_suggestion', 'no_overlap_found', applied_restrictions=applied)
    # ponytail: first candidate with a bounded, clean work duration, else the first; ranking by saving needs reviewed timings
    c = min(found.candidates, key=lambda x: (x.work_minutes is None or bool(x.timing_reasons), found.candidates.index(x)))
    a, w = int(c.anchor_step_id.split(':')[1]), int(c.work_step_id.split(':')[1])
    stage, adjacent = _stage_name(c.anchor_quote), w == a + 1
    # ponytail: neutral wording only; the food-naming "While the X roasts" clause guesses the food from the ingredient list and can be wrong
    prefix = f'Once the preceding {stage} stage has started, begin this step: ' if adjacent else f'After starting the {stage} stage in step {a + 1}, begin this step: '
    new_steps = [prefix + t if i == w else t for i, t in enumerate(steps)]
    tip = f'You can start step {w + 1} while the {stage} in step {a + 1} is running instead of waiting for it; no time saving is verified.'
    metric, est = None, llm and estimate_minutes(title, steps, w, llm)
    if est:  # the work runs inside the wait, so it saves at most the wait's lower bound
        lo, hi = (round(min(x, c.window_minutes.lower)) for x in est)
        metric = {'name': 'minutes_saved', 'unit': 'min', 'lower': lo, 'upper': hi}
        tip = f'Start step {w + 1} while the {stage} in step {a + 1} is running: it saves about {lo if lo == hi else f"{lo} to {hi}"} min (model estimate, indicative).'
    row = _row(recipe_id, region, 'adapted', applied_restrictions=applied)
    row.update(
        original=dict(title=title, lines=lines, steps=steps),
        adapted=dict(title=title, lines=lines, steps=new_steps, changed_positions=[], changed_steps=[w]),
        swap={'operation': 'OVERLAP_STEPS', 'anchor_step': a, 'work_step': w, 'anchor_quote': c.anchor_quote},
        benefit={'facet': 'time', 'tip': tip, 'tip_source': 'code', 'metric': metric, 'swap': [], 'notes': [] if metric else ['start_only_no_time_saving_claim']},
        provenance={'source': SOURCE, 'text': 'code_start_only: overlap inferred by code from the step text',
                    'flags': {'unreviewed': True, 'start_only': True, **({'indicative': True} if metric else {})},
                    'timing': {'window_minutes': c.window_minutes.model_dump(mode='json'),
                               'work_minutes': c.work_minutes.model_dump(mode='json') if c.work_minutes else None, 'reasons': c.timing_reasons}})
    if llm:
        row['provenance']['llm'] = {'status': 'ok' if metric else 'estimate_failed'}
    return row
