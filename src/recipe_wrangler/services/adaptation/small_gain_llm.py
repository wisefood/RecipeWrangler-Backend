"""LLM stages for small-gain adaptation: culinary choice between candidates, then one structured rewrite.

Call 1 picks the most culinarily sensible candidate (or none) after seeing each candidate's before/after nutrient table.
Call 2 returns title, instructions (as a list tied to the original steps), reworded changed ingredient lines and notes.
The model never invents numbers: every output is validated in code and a failed validation gets one retry.
The LLM is injected as `llm(messages, schema_name, max_tokens) -> raw JSON text`, so everything here runs offline in tests.
`CappedOpenRouter` is the only real client: hosted OpenRouter only, never a local model, with a durable cost cap.
"""
from __future__ import annotations

import difflib
import fcntl
import json
import os
import re
import time
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import small_gain as sg

MAX_TOKENS = 3000
UNITS = {'°': 'deg', 'degrees': 'deg', 'degree': 'deg', 'minutes': 'min', 'minute': 'min', 'mins': 'min', 'min': 'min',
         'hours': 'h', 'hour': 'h', 'hrs': 'h', 'hr': 'h', 'h': 'h', 'g': 'g', 'kg': 'kg', 'ml': 'ml', 'l': 'l',
         'tablespoons': 'tbsp', 'tablespoon': 'tbsp', 'tbsp': 'tbsp', 'teaspoons': 'tsp', 'teaspoon': 'tsp', 'tsp': 'tsp',
         'dessertspoons': 'dsp', 'dessertspoon': 'dsp', 'cup': 'cup', 'cups': 'cup', 'oz': 'oz', 'lb': 'lb'}
FRACTIONS = {'½': '0.5', '¼': '0.25', '¾': '0.75', '⅓': '0.33', '⅔': '0.67'}
QUANTITY = re.compile(r'(\d+(?:[.,]\d+)?|[½¼¾⅓⅔])\s*(°\s?[CF]?|degrees?|minutes?|mins?|hours?|hrs?|kg|ml|tablespoons?|tbsp|teaspoons?|tsp|dessertspoons?|cups?|oz|lb|g|l|h)(?![a-z])', re.I)


class ChoiceOut(BaseModel):
    model_config = ConfigDict(extra='forbid')
    choice: int | None = Field(description='candidate id, or null when no candidate makes culinary sense')
    ranking: list[int] = Field(default_factory=list)
    reason: str = Field(min_length=3, max_length=500)


class StepOut(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_steps: list[int] = Field(min_length=1, description='1-based numbers of the original steps this step covers')
    text: str = Field(min_length=3)


class LineOut(BaseModel):
    model_config = ConfigDict(extra='forbid')
    position: int
    new: str = Field(min_length=2)


class NoteOut(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['time', 'temperature', 'method', 'other']
    text: str = Field(min_length=3)


class RewriteOut(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=2, max_length=160)
    instructions: list[StepOut] = Field(min_length=1)
    ingredient_lines: list[LineOut] = Field(default_factory=list)
    notes: list[NoteOut] = Field(default_factory=list)
    tip: str = Field(default='', max_length=240, description='one friendly sentence for the cook: what changed and why it helps; '
                     'write {benefit} exactly once where the measured gain goes; no digits')


CHOICE_SYSTEM = (
    'You are a careful recipe editor. You get a recipe and several candidate adaptations that already passed nutrition checks. '
    'Choose the ONE candidate that gives the best result a good cook would actually make. Weigh two things together: the size of the '
    'nutrition gain in the before/after table, and whether the dish still works. Prefer a meaningful gain over a token change: do not '
    'pick a small cut when a realistic candidate gives a much bigger improvement. Replacing the main ingredient (for example lamb with '
    'turkey or chicken breast) is acceptable when the result is still a sensible dish; the dish is then simply renamed. Reject a '
    'candidate only if it would not work in the dish. Answer choice=null only if none works. Never invent candidates or numbers. '
    'Return JSON only, in the response_schema format.')
REWRITE_SYSTEM = (
    'You are a careful recipe editor. Apply exactly the one chosen change and nothing else. Return JSON in the response_schema format: '
    'the new title, the full instructions as a list where every original step is covered (source_steps), the reworded changed '
    'ingredient lines, and notes. The input steps are numbered only so you can refer to them in source_steps; never write a step number or any list numbering inside a step text. Rules: keep every quantity and ingredient that the change does not touch; never add a quantity that '
    'is not in the original steps or the changed ingredient line; if the change needs a different time or temperature, say so in a '
    'note of kind time or temperature and use only that value; rename the dish only when the main food changes (for example lamb to '
    'turkey); no step may still mention a removed food; ingredient_lines contains only the changed lines, each with just position '
    'and new (the full new ingredient line, written naturally like a cookbook, never with database words). Bad: "2.5 g Oil, olive", '
    '"turkey breast, skinless boneless raw, diced", "250g of pork, loin medallions, raw, lean", "100 g Cheese, cottage, plain, reduced fat". '
    'Good: "2.5 ml olive oil", "450g turkey breast, diced", "250g lean pork loin medallions", "100g reduced-fat cottage cheese". '
    'In the steps, never restate or convert a quantity of a changed ingredient (write "the oil", not "0.5 tsp oil"); keep the unit of the original line; when a line gives a second unit in brackets, drop the bracket. Always return an empty notes list (notes are written elsewhere). For a wholemeal or wholewheat swap keep the original shape or kind (penne stays penne: "wholewheat penne", not "wholewheat spaghetti"). Copy every step word for word: change only the words that name a changed or swapped food or its quantity. Do not fix typos, spelling, headings, punctuation or wording, and do not merge, split, drop or reorder steps: exactly one output step per input step, with source_steps equal to [n]. '
    'tip: one friendly sentence for the cook saying what was swapped or cut and why it is better; write the placeholder {benefit} exactly '
    'once where the gain goes (code fills in the measured number) and use no digits anywhere else in the tip.')
DB_WORDING = re.compile(r'(?i:\b(?:raw|skinless|boneless|without (?:bone|skin)|reduced fat|plain,))|^[^,]*\b[A-Z][a-z]+, [a-z]')
STEP_NUMBER = re.compile(r'^\s*(?:step\s*)?\d+\s*[.):]\s+', re.I)
FILLER_NOTE = re.compile(r'as requested|as instructed|as specified', re.I)
EXAMPLE_OUTPUT = {'title': 'Turkey curry', 'instructions': [{'source_steps': [1], 'text': 'Brown the turkey for 5 minutes.'},
                                                          {'source_steps': [2, 3], 'text': 'Add the onions and simmer 25 minutes.'}],
                  'ingredient_lines': [{'position': 1, 'new': '450g turkey breast, diced'}],
                  'notes': [{'kind': 'time', 'text': 'Turkey breast needs 25 minutes, not 90.'}],
                  'tip': 'Turkey breast keeps the curry just as hearty while {benefit}.'}


def flat_steps(instructions) -> list[str]:
    steps = instructions if isinstance(instructions, list) else [instructions or '']
    return [line.strip() for s in steps for line in str(s).split('\n') if line.strip()]


def quantities(text: str) -> set[tuple[str, str]]:
    out = set()
    for number, unit in QUANTITY.findall(text or ''):
        u = unit.lower().replace(' ', '')
        u = 'deg' if u.startswith('°') else UNITS.get(u, UNITS.get(u.rstrip('s'), u))
        n = FRACTIONS.get(number, number.replace(',', '.'))
        out.add((str(float(n)).rstrip('0').rstrip('.') if '.' in n else n, u))
    return out


def metric(q: set) -> set:
    return {x for x in q if x[1] not in ('oz', 'lb')}  # a leftover imperial amount is not checked, the metric one is


def close(a: set, b: set, tol=0.12) -> bool:
    """Same units and every number within tol (6.7 g vs 7 g)."""
    return {u for _, u in a} == {u for _, u in b} and all(any(u == u2 and abs(float(n) - float(n2)) <= tol * float(n2) + 0.05 for n2, u2 in b) for n, u in a)


QTY_PREFIX = re.compile(r'^(\s*(?:[\d.,/½¼¾⅓⅔]+\s*(?:/\s*[\d.,]+\s*(?:oz|lb|g)\.?\s*)?(?:g|ml|kg|l|tbsp|tsp|oz|lb|fl oz|tablespoons?|teaspoons?|cups?)?\.?\s*(?:of\s+)?)?)(.*)$', re.I)
NOISE = re.compile(r'(?i)\b(raw|iodine added|no fl?uo?ri?de added|average|(?<![-\d])\d+% fat( minimum)?)\b')


def cookbook_line(text: str) -> str:
    """Deterministic cookbook wording: 'Oil, olive' -> 'olive oil', drop 'raw', 'skinless boneless raw' and bracketed database notes.
    A lowercase line is only stripped of noise words; a 'Head, qualifier, ...' line is reordered to 'qualifier head'. Quantities are untouched."""
    prefix, name = QTY_PREFIX.match(text).groups()
    name = re.sub(r'\s*\([^)]*\)', '', name)
    name = re.sub(r'(?i)\bskinless,? boneless\b,?|\bwithout (?:bone|skin)\b( and (?:bone|skin))?,?', '', name)
    parts = [p.strip() for p in re.split(r',\s*', name) if p.strip() and not NOISE.fullmatch(NOISE.sub(lambda m: m.group(1), p.strip()))]
    parts = [NOISE.sub('', p).strip() for p in parts]
    parts = [p for p in parts if p and p.lower() not in ('plain', 'white') or len(parts) == 1]
    if len(parts) > 1 and re.fullmatch(r'[A-Z][a-z]+', parts[0]):
        name = ' '.join(reversed(parts[1:]) if len(parts) > 2 else parts[1:]) + ' ' + parts[0].lower()
    else:
        name = ', '.join(parts)
    name = re.sub(r'\s+', ' ', name).strip()
    return (prefix + (name[0].lower() + name[1:] if name[:2].istitle() else name)).strip()


SHAPES = r'\b(penne|fusilli|spaghetti|macaroni|tagliatelle|linguine|farfalle|rigatoni|conchiglie|orzo|lasagne|noodles|pasta shells)\b'
STOP = {'the', 'a', 'an', 'of', 'and', 'to', 'some', 'your', 'it', 'this', 'that'}


def words(text: str) -> list[str]:
    return re.findall(r'\w+', (text or '').casefold())


def stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith('s') else w


def unexplained_edits(old: str, new: str, allowed: set[str]) -> list[str]:
    """Words added to or removed from a step that have nothing to do with the chosen change (typo fixes, dropped headings, rewording)."""
    a, b = words(old), words(new)
    return [w for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if tag != 'equal'
            for w in a[i1:i2] + b[j1:j2] if w not in STOP and stem(w) not in allowed]


def swap_notes(candidate: dict) -> list[dict]:
    """Cooking note written in code (never by the model) for a lean-poultry swap."""
    if not candidate['flags']['changes_dish_identity']:
        return []
    return [{'kind': 'method', 'text': f'{new.capitalize()} is leaner than {old} and cooks faster: cook until just done and check early, because it dries out if overcooked.'}
            for old, new in swap_words(candidate)]


def database_style(line: str, matched_label: str | None) -> bool:
    """'80 g Celery, raw' or '30 g Lard' (the line is the database name) as opposed to a cookbook line ('450g boneless pork')."""
    name = QTY_PREFIX.match(line or '').group(2).strip()
    return name.casefold() == (matched_label or '').strip().casefold() or bool(re.match(r'[A-Z][a-z]+, [a-z]', name))


def display_line(old: str, model_new: str, suggested_new: str, swap: bool, candidate_label: str | None, matched_label: str | None = None) -> str:
    """The ingredient line the user sees. A database-style source line is matched exactly by the profiler, so it keeps the
    database name and only the amount or food changes; a cookbook line gets the model's wording, cleaned."""
    if database_style(old, matched_label):
        line = QTY_PREFIX.match(old).group(1) + candidate_label if swap and candidate_label else suggested_new
    else:
        line = cookbook_line(model_new)
    return re.sub(r'(^|\s)(\d+\s)?(1|½|¼|¾|⅓|⅔) (tablespoon|teaspoon|cup|dessertspoon)s\b',
                  lambda m: m.group(0) if m.group(2) else f'{m.group(1)}{m.group(3)} {m.group(4)}', line)  # '½ tablespoons' -> '½ tablespoon', '1 ½ tablespoons' stays


def first_word(label: str) -> str:
    m = re.match(r"\s*([A-Za-z]+)", label or '')
    return m.group(1).lower() if m else ''


ANIMAL = {'red_meat', 'poultry', 'fish'}


def food_word(label: str) -> str:
    """The word that names the food ('Brook trout' -> trout, 'Turkey breast skinless' -> turkey)."""
    from .food_roles import role_of
    for token in re.findall(r"[A-Za-z]+", label or ''):
        if role_of(token):
            return token.lower()
    return first_word(label)


def swap_words(candidate: dict) -> list[tuple[str, str]]:
    """Main-food swaps that rename the dish and must leave no mention of the old food: any meat or fish for another meat or fish."""
    from .food_roles import role_of
    out = []
    for c in candidate['changes']:
        if c.get('operation') == 'REDUCE_AMOUNT' or not c.get('candidate_label'):
            continue
        old = food_word(c.get('source_line') or c['matched_label']) if role_of(c.get('source_line') or '') in ANIMAL else food_word(c['matched_label'])
        new = food_word(c['candidate_label'])
        if old != new and (c.get('category') == 'protein_swap' or (role_of(c['matched_label']) in ANIMAL and role_of(c['candidate_label']) in ANIMAL)):
            out.append((old, new))
    return out


def candidate_lines(candidate: dict, lines: list[dict]) -> list[dict]:
    """Deterministic suggested new text for each changed ingredient line. lines: {position, text, weight_g}."""
    by_pos = {item['position']: item for item in lines}
    out = []
    for ch in candidate['changes']:
        for pos in ch['position'] if isinstance(ch['position'], list) else [ch['position']]:
            item = by_pos[pos]
            frac = ch.get('reduction_fraction') or 0
            new, note = sg.rewrite(dict(ch, source_line=item['text'], weight_before_g=item['weight_g'], weight_after_g=item['weight_g'] * (1 - frac)))
            if ch['operation'] not in ('REDUCE_AMOUNT', 'INCREASE_VEG'):
                new = 'use ' + new.replace('**', '').split('  →  use ')[-1]
            out.append({'position': pos, 'old': item['text'], 'suggested_new': new + (f' ({note})' if note else ''), 'swap': ch['operation'] not in ('REDUCE_AMOUNT', 'INCREASE_VEG'), 'label': ch.get('candidate_label'), 'matched': ch.get('matched_label')})
    return out


def final_line_problems(candidate: dict, lines: list[dict], final_lines: list[dict]) -> list[str]:
    """Safety net: the finished ingredient lines must still pass the allergen/diet gate against the original recipe."""
    ops = {p: ch['operation'] for ch in candidate['changes'] for p in (ch['position'] if isinstance(ch['position'], list) else [ch['position']])}
    parts = [{'operation': ops[l['position']], 'candidate_label': l['new']} for l in final_lines if ops.get(l['position']) != 'REDUCE_AMOUNT']
    return sg.restriction_problems(parts, [x['text'] for x in lines])


def fallback_title(title: str, swaps: list[tuple[str, str]]) -> str:
    for old, new in swaps:
        title = re.sub(rf'\b{old}\b', lambda m: new.capitalize() if m.group(0)[0].isupper() else new, title, flags=re.I)
    return title


def choice_messages(title, lines, steps, pack):
    user = {'title': title, 'ingredients': [item['text'] for item in lines], 'steps': [f'{i}. {s}' for i, s in enumerate(steps, 1)],
            'candidates': [{'id': c['id'], 'change': c['summary'], 'flags': c['flags'], 'grade': f"{c['grade_before'][-1]} -> {c['grade_after'][-1]}",
                            'per_serving_before': c['before_per_serving'], 'per_serving_after': c['after_per_serving']} for c in pack],
            'response_schema': ChoiceOut.model_json_schema()}
    return [{'role': 'system', 'content': CHOICE_SYSTEM}, {'role': 'user', 'content': json.dumps(user, ensure_ascii=False)}]


def rewrite_messages(title, lines, steps, candidate, changed, problems=None):
    user = {'title': title, 'ingredients': [item['text'] for item in lines], 'steps': [f'{i}. {s}' for i, s in enumerate(steps, 1)],
            'chosen_change': candidate['summary'],
            'changed_ingredient_lines_input': [{'position': c['position'], 'current': c['old'], 'proposed': c['suggested_new']} for c in changed],
            'example_output_format_only': EXAMPLE_OUTPUT,
            'per_serving_before': candidate['before_per_serving'], 'per_serving_after': candidate['after_per_serving'],
            'response_schema': RewriteOut.model_json_schema()}
    if problems:
        user['previous_answer_was_rejected_because'] = problems
    return [{'role': 'system', 'content': REWRITE_SYSTEM}, {'role': 'user', 'content': json.dumps(user, ensure_ascii=False)}]


def validate_choice(out: ChoiceOut, pack: list[dict]) -> list[str]:
    ids = {c['id'] for c in pack}
    problems = []
    if out.choice is not None and out.choice not in ids:
        problems.append(f'choice {out.choice} is not a candidate id')
    problems += [f'ranking id {i} is not a candidate id' for i in out.ranking if i not in ids]
    return problems


def restore_unrelated_steps(out: RewriteOut, steps: list[str], candidate: dict, changed: list[dict]) -> None:
    """A step the model reworded without reason (typo fix, dropped heading) goes back to the original text verbatim."""
    wanted = [c['position'] for c in changed]
    if len(out.ingredient_lines) == len(wanted) and {l.position for l in out.ingredient_lines} != set(wanted):
        free = {c['position']: set(map(stem, words(c['old']))) for c in changed}
        for line in sorted(out.ingredient_lines, key=lambda l: l.position in free, reverse=True):
            if line.position in free:
                free.pop(line.position)
                continue
            best = max(free, key=lambda p: len(free[p] & set(map(stem, words(line.new)))))
            if free[best] & set(map(stem, words(line.new))):
                line.position = best
                free.pop(best)
    if [s.source_steps for s in out.instructions] != [[i] for i in range(1, len(steps) + 1)]:
        return
    allowed = {stem(w) for t in [c['old'] + ' ' + c['suggested_new'] for c in changed] + [l.new for l in out.ingredient_lines]
               + [x for ch in candidate['changes'] for x in (ch.get('matched_label'), ch.get('candidate_label'), ch.get('source_line')) if x] for w in words(t)}
    for i, step in enumerate(out.instructions):
        if unexplained_edits(STEP_NUMBER.sub('', steps[i]), STEP_NUMBER.sub('', step.text), allowed):
            step.text = steps[i]


def validate_rewrite(out: RewriteOut, title: str, steps: list[str], candidate: dict, changed: list[dict]) -> list[str]:
    problems, swaps = [], swap_words(candidate)
    # title: renamed only when the main food changes
    if swaps:
        for old, new in swaps:
            if re.search(rf'\b{old}\b', title, re.I) and (not re.search(rf'\b{new}\b', out.title, re.I) or re.search(rf'\b{old}\b', out.title, re.I)):
                problems.append(f'title must replace "{old}" with "{new}"')
    elif out.title.strip().lower() != title.strip().lower():
        problems.append('title must stay unchanged because the main food does not change')
    # every original step is covered, no unknown step numbers
    covered = {i for s in out.instructions for i in s.source_steps}
    if covered - set(range(1, len(steps) + 1)):
        problems.append('source_steps contains unknown step numbers')
    if set(range(1, len(steps) + 1)) - covered:
        problems.append('original steps not covered: ' + ', '.join(str(i) for i in sorted(set(range(1, len(steps) + 1)) - covered)))
    if [s.source_steps for s in out.instructions] != [[i] for i in range(1, len(steps) + 1)]:
        problems.append('write exactly one output step per input step, in order, with source_steps equal to [n]')
    else:
        allowed = {stem(w) for t in [c['old'] + ' ' + c['suggested_new'] for c in changed] + [l.new for l in out.ingredient_lines]
                   + [x for ch in candidate['changes'] for x in (ch.get('matched_label'), ch.get('candidate_label'), ch.get('source_line')) if x] for w in words(t)}
        for i, step in enumerate(out.instructions):
            bad = unexplained_edits(STEP_NUMBER.sub('', steps[i]), STEP_NUMBER.sub('', step.text), allowed)
            if bad:
                problems.append(f'step {i + 1} changes words unrelated to the chosen change ({", ".join(sorted(set(bad))[:6])}); copy it exactly')
    # a wholemeal swap keeps the pasta shape of the recipe (penne stays penne, even if the database record is spaghetti)
    recipe_text = ' '.join(steps + [c['old'] for c in changed]).casefold()
    new_text = ' '.join([l.new for l in out.ingredient_lines] + [s.text for s in out.instructions]).casefold()
    added = set(re.findall(SHAPES, new_text)) - set(re.findall(SHAPES, recipe_text))
    if added:
        problems.append(f'keep the pasta shape the recipe uses; do not introduce {", ".join(sorted(added))}')
    # ingredient lines: only changed positions, all of them, quantities as expected
    wanted = {c['position']: c for c in changed}
    got = {line.position: line for line in out.ingredient_lines}
    if set(got) - set(wanted):
        problems.append('ingredient_lines changes a line that is not part of the chosen change')
    for pos, c in wanted.items():
        line = got.get(pos)
        if line is None:
            problems.append(f'ingredient line {pos} is missing')
            continue
        old_q, new_q, line_q = quantities(c['old']), quantities(c['suggested_new']), quantities(line.new)
        if c['swap'] and line_q != old_q:
            problems.append(f'ingredient line {pos} must keep the original quantities {sorted(old_q)}')
        elif not c['swap'] and metric(new_q) and not close(metric(line_q), metric(new_q)):
            problems.append(f'ingredient line {pos} must use the reduced quantities {sorted(new_q)}')
    # numbers: nothing invented
    noted = ' '.join(n.text for n in out.notes)
    allowed = quantities(' '.join(steps)) | quantities(noted) | quantities(' '.join(c['suggested_new'] for c in changed)) | quantities(' '.join(l.new for l in out.ingredient_lines))
    invented = quantities(' '.join(s.text for s in out.instructions)) - allowed
    if invented:
        problems.append('invented quantities not in the original steps or notes: ' + ', '.join(f'{n} {u}' for n, u in sorted(invented)))
    if out.tip and (out.tip.count('{benefit}') != 1 or re.search(r'\d', out.tip.replace('{benefit}', ''))):
        problems.append('tip must contain {benefit} exactly once and no digits')
    # removed foods must not survive, new food must appear
    text = ' '.join([s.text for s in out.instructions] + [l.new for l in out.ingredient_lines])
    for old, new in swaps:
        if re.search(rf'\b{old}\b', text, re.I):
            problems.append(f'"{old}" is still mentioned')
        if not re.search(rf'\b{new}\b', text, re.I):
            problems.append(f'"{new}" never appears in the steps or ingredient lines')
    return problems


LAST_RAW: dict = {}  # last raw model answer per stage, kept for failure reports


LABELS = {'saturated_fat_g': 'saturated fat', 'sugar_g': 'sugar', 'sodium_mg': 'salt', 'energy_kcal': 'calories', 'fat_g': 'fat'}


def benefit_metric(candidate: dict) -> dict:
    """The measured gain of a candidate, computed in code from its before/after per-serving values (never by the model)."""
    b, a = candidate.get('before_per_serving') or {}, candidate.get('after_per_serving') or {}
    if candidate.get('tier') == 'cost':
        return dict(facet='cost', name='cost_eur_per_serving', unit='EUR', before=b['cost_eur'], after=a['cost_eur'],
                    change=round(a['cost_eur'] - b['cost_eur'], 2), text=f"it saves €{b['cost_eur'] - a['cost_eur']:.2f} per serving")
    if candidate.get('tier') == 'co2':
        cut = b['co2_kg'] - a['co2_kg']
        return dict(facet='sustainability', name='co2e_kg_per_serving', unit='kg CO2e', before=b['co2_kg'], after=a['co2_kg'],
                    change=round(-cut, 3), text=f"it cuts {cut:.2f} kg CO2e per serving ({cut / b['co2_kg']:.0%} less)")
    drops = {m: (b[m] - a[m]) / b[m] for m in LABELS if b.get(m) and a.get(m) is not None and a[m] < b[m]}
    top = max(drops, key=drops.get) if drops else None
    parts = []
    if candidate['grade_after'] != candidate['grade_before']:
        parts.append(f"the Nutri-Score improves from {candidate['grade_before'][-1]} to {candidate['grade_after'][-1]}")
    if top:
        parts.append(f"it has {drops[top]:.0%} less {LABELS[top]} per serving")
    return dict(facet='nutrition', name=top or 'nutri_score', unit='per serving', before=b.get(top) if top else candidate['grade_before'],
                after=a.get(top) if top else candidate['grade_after'], change=round(a[top] - b[top], 3) if top else None,
                grade_before=candidate['grade_before'], grade_after=candidate['grade_after'], text=' and '.join(parts) or 'it is a little healthier')


def benefit_block(candidate: dict, tip: str, notes: list[dict]) -> dict:
    """UI facet: free-text tip from the model with the code-measured gain filled in; falls back to a code sentence."""
    metric = benefit_metric(candidate)
    swaps = [{'from': food_word(ch.get('matched_label') or ch.get('source_line') or ''), 'to': food_word(ch['candidate_label']),
              'from_record': ch.get('matched_label'), 'to_record': ch['candidate_label']} for ch in candidate['changes'] if ch.get('candidate_label')]
    fallback = (f"Swap the {' and '.join(x['from'] for x in swaps)} for {' and '.join(x['to'] for x in swaps)}: {metric['text']}." if swaps
                else f"{candidate['summary'].split(';')[0]}: {metric['text']}.")
    text = tip.replace('{benefit}', metric['text']) if tip else fallback
    return {'facet': metric['facet'], 'tip': text, 'tip_source': 'llm' if tip else 'code', 'metric': {k: v for k, v in metric.items() if k not in ('text', 'facet')},
            'swap': swaps, 'notes': notes}


def _call(llm, messages, model_cls, validate, calls):
    """One call plus at most one retry carrying the validation problems back. Returns (parsed|None, problems)."""
    problems = []
    for attempt in range(2):
        if attempt:
            messages = messages[:-1] + [{**messages[-1], 'content': messages[-1]['content'] + '\n\nYour previous answer was rejected: ' + '; '.join(problems)}]
        calls.append(model_cls.__name__)
        raw = llm(messages, model_cls.__name__, MAX_TOKENS)
        LAST_RAW[model_cls.__name__] = raw
        try:
            parsed = model_cls.model_validate_json(raw)
        except (ValidationError, ValueError) as error:
            problems = [f'not valid JSON for the schema: {str(error)[:200]}']
            continue
        problems = validate(parsed)
        if not problems:
            return parsed, []
    return None, problems


def run_llm_stages(result: dict, recipe: dict, lines: list[dict], llm) -> dict:
    """lines: [{position, text, weight_g}] in recipe order. Returns an unsaved draft or an explicit reason."""
    pack = result.get('candidate_pack') or []
    out = {'recipe_id': result['recipe_id'], 'original_title': result['title'], 'unsaved': True, 'calls': 0}
    if result.get('outcome') != 'adapted' or not pack:
        return dict(out, status='no_candidates')
    title, steps, calls = result['title'], flat_steps(recipe.get('instructions')), []
    choice, problems = _call(llm, choice_messages(title, lines, steps, pack), ChoiceOut, lambda o: validate_choice(o, pack), calls)
    out['calls'] = len(calls)
    if choice is None:
        return dict(out, status='choice_failed', failure=problems)
    out['choice'] = {'id': choice.choice, 'ranking': choice.ranking, 'reason': choice.reason}
    if choice.choice is None:
        return dict(out, status='no_culinary_candidate')
    candidate = next(c for c in pack if c['id'] == choice.choice)
    changed = candidate_lines(candidate, lines)
    rewrite, problems = _call(llm, rewrite_messages(title, lines, steps, candidate, changed), RewriteOut,
                              lambda o: (restore_unrelated_steps(o, steps, candidate, changed), validate_rewrite(o, title, steps, candidate, changed))[1], calls)
    out['calls'] = len(calls)
    out['candidate'] = candidate
    if rewrite is None:
        swaps = swap_words(candidate)
        return dict(out, status='rewrite_failed', failure=problems, last_raw=LAST_RAW.get('RewriteOut'), fallback_title=fallback_title(title, swaps))
    by_pos = {c['position']: c for c in changed}
    final_lines = [dict(l.model_dump(), new=display_line(by_pos[l.position]['old'], l.new, by_pos[l.position]['suggested_new'], by_pos[l.position]['swap'], by_pos[l.position].get('label'), by_pos[l.position].get('matched')), model_new=l.new) for l in rewrite.ingredient_lines]
    bad = final_line_problems(candidate, lines, final_lines)
    if bad:
        return dict(out, status='restriction_failed', failure=bad)
    notes = swap_notes(candidate)
    return dict(out, status='ok', title=rewrite.title, title_changed=rewrite.title.strip().lower() != title.strip().lower(),
                instructions=[re.sub(r'\b1 (tablespoon|teaspoon|cup|dessertspoon)s\b', r'1 \1', STEP_NUMBER.sub('', s.text)) for s in rewrite.instructions], ingredient_lines=final_lines,
                notes=notes, benefit=benefit_block(candidate, rewrite.tip, notes))


class TimeTipOut(BaseModel):
    model_config = ConfigDict(extra='forbid')
    step_text: str = Field(min_length=3, description='the work step with a short lead-in; the original text follows unchanged')
    prep_minutes_low: int = Field(ge=1, le=90, description='hands-on minutes the work step takes for this recipe')
    prep_minutes_high: int = Field(ge=1, le=90)
    tip: str = Field(min_length=10, max_length=240, description='one sentence; write {minutes} where the saving goes, no digits')


TIME_SYSTEM = (
    'You are a careful recipe editor. Code found that one step (the work step) can be done while an earlier step cooks unattended '
    '(the anchor step), and checked that the work does not need the anchor\'s food or equipment. Return JSON in the response_schema '
    'format. step_text: a short natural lead-in that names what is cooking (for example "While the potatoes roast,"), then the work step '
    'copied word for word (only its first letter may become lower case). Do not change, fix or shorten the step itself and write no '
    'numbers in the lead-in. prep_minutes_low/high: your honest estimate of the hands-on minutes the work step takes for this recipe. '
    'tip: one friendly sentence for the cook saying what to do in parallel and why it saves time; write the placeholder {minutes} '
    'exactly once where the time saved goes and use no digits anywhere else (code fills in the number).')
TIME_EXAMPLE = {'step_text': 'While the rice simmers, slice the peppers.', 'prep_minutes_low': 3, 'prep_minutes_high': 5,
                'tip': 'Slice the peppers while the rice simmers instead of afterwards, so dinner is ready {minutes} sooner.'}


def minutes_text(low: int, high: int) -> str:
    return f'about {low} minutes' if low == high else f'{low}-{high} minutes'


def validate_time_tip(out: TimeTipOut, steps: list[str], overlap: dict) -> list[str]:
    work = steps[overlap['work_step'] - 1]
    body = work[0].lower() + work[1:]
    problems = []
    if not out.step_text.endswith(body):
        problems.append('step_text must end with the work step copied word for word (only the first letter lower case)')
    else:
        lead = out.step_text[:-len(body)]
        if re.search(r'\d', lead):
            problems.append('no numbers in the lead-in')
        if not re.search(r'\b(?:while|meanwhile|during)\b', lead, re.I):
            problems.append('the lead-in must say the work happens while the anchor step cooks')
        if len(words(lead)) > 10:
            problems.append('lead-in too long (max 10 words)')
    if out.prep_minutes_low > out.prep_minutes_high:
        problems.append('prep_minutes_low > prep_minutes_high')
    if out.tip.count('{minutes}') != 1 or re.search(r'\d', out.tip.replace('{minutes}', '')):
        problems.append('tip must contain {minutes} exactly once and no digits')
    return problems


def run_time_tip(title: str, steps: list[str], overlap: dict, llm) -> dict:
    """overlap: {anchor_step, work_step (1-based), anchor_quote, window_minutes, code_step}. Minutes saved = min(prep, window), by code."""
    payload = {'title': title, 'steps': {i + 1: s for i, s in enumerate(steps)}, **overlap, 'example_output': TIME_EXAMPLE}
    messages = [{'role': 'system', 'content': TIME_SYSTEM},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False) + '\nresponse_schema: ' + json.dumps(TimeTipOut.model_json_schema())}]
    calls = []
    out, problems = _call(llm, messages, TimeTipOut, lambda o: validate_time_tip(o, steps, overlap), calls)
    if out is None:
        return {'status': 'time_tip_failed', 'failure': problems, 'calls': len(calls), 'fallback_step': overlap['code_step']}
    window = overlap['window_minutes']
    low, high = min(out.prep_minutes_low, window), min(out.prep_minutes_high, window)
    saved = minutes_text(int(low), int(high))
    minutes = {'low': low, 'high': high, 'basis': 'model estimate of hands-on prep, capped by the source cooking window',
               'window_minutes': window, 'prep_estimate': [out.prep_minutes_low, out.prep_minutes_high]}
    return {'status': 'ok', 'calls': len(calls), 'step_text': out.step_text, 'tip': out.tip.replace('{minutes}', saved), 'minutes_saved': minutes,
            'benefit': {'facet': 'time', 'tip': out.tip.replace('{minutes}', saved), 'tip_source': 'llm',
                        'metric': {'name': 'minutes_saved', 'unit': 'min', 'low': low, 'high': high, 'basis': minutes['basis']},
                        'swap': [], 'notes': []}}


class BudgetExceeded(RuntimeError):
    pass


class CappedOpenRouter:
    """Hosted OpenRouter only (never a local model), every attempt reserved in a durable ledger before dispatch, no retries."""

    def __init__(self, ledger_path, cap_usd, max_attempts, *, model='meta-llama/llama-3.3-70b-instruct',
                 prompt_usd_per_m='0.20', completion_usd_per_m='0.60', client_factory=None):
        self.path, self.cap, self.max_attempts, self.model = Path(ledger_path), Decimal(str(cap_usd)), max_attempts, model
        self.rate_in, self.rate_out, self.case = Decimal(prompt_usd_per_m), Decimal(completion_usd_per_m), None
        self.client_factory = client_factory

    def _config(self):
        if os.getenv('ADAPT_LLM_SOURCE', '').strip().lower() != 'openrouter':
            raise BudgetExceeded('ADAPT_LLM_SOURCE must be openrouter: local models are not allowed')
        from .llm_judge import _llm_config
        return _llm_config()

    def __call__(self, messages, schema_name, max_tokens):
        config = self._config()
        prompt_bytes = len(json.dumps(messages, ensure_ascii=False).encode())
        reserve = (Decimal(prompt_bytes) * self.rate_in + Decimal(max_tokens) * self.rate_out) / Decimal(1_000_000)  # bytes >= tokens
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = json.loads(self.path.read_text()) if self.path.exists() else {'cap_usd': str(self.cap), 'max_attempts': self.max_attempts, 'attempts': []}
            reserved = sum((Decimal(a['reserved_usd']) for a in ledger['attempts']), Decimal(0))
            if len(ledger['attempts']) >= self.max_attempts or reserved + reserve > self.cap:
                raise BudgetExceeded(f'budget exhausted: {len(ledger["attempts"])} attempts, ${reserved} reserved of ${self.cap}')
            entry = {'n': len(ledger['attempts']) + 1, 'case': self.case, 'schema': schema_name, 'model': self.model, 'at': time.time(),
                     'reserved_usd': str(reserve), 'status': 'reserved'}
            ledger['attempts'].append(entry)
            self.path.write_text(json.dumps(ledger, indent=1))
            fcntl.flock(lock, fcntl.LOCK_UN)
        try:
            import openai
            factory = self.client_factory or (lambda: openai.OpenAI(base_url=config['base_url'], api_key=config['api_key'], timeout=config['timeout'], max_retries=0))
            with factory() as client:
                response = client.chat.completions.create(model=self.model, temperature=0.0, max_tokens=max_tokens, messages=messages,
                                                          response_format={'type': 'json_object'}, extra_body={'reasoning': {'enabled': False}})
            usage = getattr(response, 'usage', None)
            entry.update(status='received', usage=usage.model_dump() if hasattr(usage, 'model_dump') else None, finish=response.choices[0].finish_reason)
            if response.choices[0].finish_reason != 'stop':
                raise ValueError('incomplete_response')
            return response.choices[0].message.content or ''
        except Exception as error:
            if entry['status'] != 'received':
                entry['status'] = 'failed'
            entry['error'] = type(error).__name__
            raise
        finally:
            with self.path.with_suffix('.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                ledger = json.loads(self.path.read_text())
                ledger['attempts'][entry['n'] - 1] = entry
                self.path.write_text(json.dumps(ledger, indent=1))
                fcntl.flock(lock, fcntl.LOCK_UN)
