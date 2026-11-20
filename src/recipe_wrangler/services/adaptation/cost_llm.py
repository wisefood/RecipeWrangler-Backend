"""LLM stages for cost adaptation: culinary choice between the cheaper candidates, then one structured rewrite of the changed line, title and steps.

Reuses the nutrition stages (schemas, retry, validators, line wording) from nutrition_adaptation_llm; only the choice prompt differs. The model never
invents numbers: price, saving and the tip sentence are written by code. The LLM is injected as `llm(messages, schema_name, max_tokens) -> raw JSON text`."""
import json
import os
import re

from . import nutrition_adaptation as sg
from . import nutrition_adaptation_llm as L

CHOICE_SYSTEM = (
    'You are a careful recipe editor. You get a recipe and several candidate swaps that each make it cheaper. Choose the ONE candidate that a good cook '
    'would actually make in this dish, so the result is still a sensible dish. Weigh the saving together with fit: the same kind of food in the same role, '
    'a similar cut or form, similar cooking time and flavour. Replacing the main ingredient (for example chicken with pork) is acceptable when the dish '
    'still works; the dish is then simply renamed. Reject a candidate that is a processed product instead of a raw cut, a different kind of food, or '
    'would change what the dish is (rice in a risotto, a bone-in cut at the same weight as a boneless one). Answer choice=null if none works. '
    'Do not judge nutrition. Never invent candidates or numbers. Return JSON only, in the response_schema format.')
TIP_PROBLEM = re.compile(r'^tip must|^\{benefit\} is a whole clause')  # the tip is written by code, whatever the model writes is dropped


def client():
    """The injected model, or None (code-only text). On when COST_ADAPT_LLM is true; the provider is the adaptation LLM setting shared with the other
    modes: ADAPT_LLM_SOURCE=openrouter in production (OPENROUTER_API_KEY, ADAPT_LLM_MODEL), vllm for local development (ADAPT_LLM_BASE_URL on 127.0.0.1)."""
    if os.getenv('COST_ADAPT_LLM', '').strip().lower() not in ('1', 'true', 'yes', 'on'):
        return None
    import openai
    from .llm_judge import _llm_config, _request_extra_body
    cfg = _llm_config()
    http = openai.OpenAI(base_url=cfg['base_url'], api_key=cfg['api_key'], timeout=cfg['timeout'], max_retries=0)

    def call(messages, schema_name, max_tokens):
        r = http.chat.completions.create(model=cfg['model'], temperature=0.0, max_tokens=max_tokens, messages=messages,
                                         response_format={'type': 'json_object'}, extra_body=_request_extra_body(cfg['source']))
        if r.choices[0].finish_reason != 'stop':
            raise ValueError('incomplete_response')
        return r.choices[0].message.content or ''
    return call


def pack(suggestions, offender, pos, line, weight_g, before):
    """Engine suggestions (same offender) in the candidate shape the nutrition stages expect."""
    out = []
    for i, s in enumerate(suggestions, 1):
        after = round(s['simulated_cost_per_serving_eur'], 2)
        out.append({'id': i, 'summary': f'Replace "{line}" with {s["substitute_name"]}: {round(s["cost_reduction_pct"] * 100)}% cheaper per kg, '
                                        f'{before:.2f} -> {after:.2f} EUR per serving',
                    'changes': [{'position': pos, 'operation': 'SWAP_FOOD', 'category': 'cost_swap', 'source_line': line, 'matched_label': offender,
                                 'candidate_label': s['substitute_name'], 'weight_before_g': weight_g, 'weight_after_g': weight_g}],
                    'flags': {'indicative': True}, 'before_per_serving': {'cost_eur': before}, 'after_per_serving': {'cost_eur': after}})
    return out


def choice_messages(title, lines, steps, cands):
    user = {'title': title, 'ingredients': [x['text'] for x in lines], 'steps': [f'{i}. {s}' for i, s in enumerate(steps, 1)],
            'candidates': [{'id': c['id'], 'change': c['summary'], 'flags': c['flags']} for c in cands],
            'response_schema': L.ChoiceOut.model_json_schema()}
    return [{'role': 'system', 'content': CHOICE_SYSTEM}, {'role': 'user', 'content': json.dumps(user, ensure_ascii=False)}]


def run(title, lines, steps, cands, llm, allergens=(), diet=None):
    """lines: [{position, text, weight_g}] in recipe order. Returns {'status': 'ok', 'choice', 'candidate', 'title', 'steps', 'new_lines': {position: text}}
    or {'status': <why not>, 'failure': [...]}; the caller keeps the code card on any status but ok."""
    calls = []
    choice, problems = L._call(llm, choice_messages(title, lines, steps, cands), L.ChoiceOut, lambda o: L.validate_choice(o, cands), calls)
    if choice is None:
        return {'status': 'choice_failed', 'failure': problems}
    out = {'choice': {'id': choice.choice, 'ranking': choice.ranking, 'reason': choice.reason}}
    if choice.choice is None:
        return dict(out, status='no_culinary_candidate')
    cand = next(c for c in cands if c['id'] == choice.choice)
    changed = L.candidate_lines(cand, lines)
    check = lambda o: [p for p in (L.restore_unrelated_steps(o, steps, cand, changed), L.fix_stale_quantities(o, changed), L.fix_pasta_shape(o, steps, changed),  # noqa: E731
                                   L.fix_leftover_swap_words(o, cand), L.validate_rewrite(o, title, steps, cand, changed, lines))[4] if not TIP_PROBLEM.match(p)]
    rewrite, problems = L._call(llm, L.rewrite_messages(title, lines, steps, cand, changed), L.RewriteOut, check, calls)
    out.update(candidate=cand, calls=len(calls))
    if rewrite is None:
        return dict(out, status='rewrite_failed', failure=problems)
    by_pos = {c['position']: c for c in changed}
    final = {x.position: L.display_line(by_pos[x.position]['old'], x.new, by_pos[x.position]['suggested_new'], by_pos[x.position]['swap'], by_pos[x.position].get('label'),
                                        by_pos[x.position].get('matched'), by_pos[x.position].get('scaled', False), by_pos[x.position].get('partial', False),
                                        by_pos[x.position].get('weight_g')) for x in rewrite.ingredient_lines}
    parts = [{'operation': 'SWAP_FOOD', 'candidate_label': t, 'matched_label': by_pos[p].get('matched')} for p, t in final.items()]
    bad = sg.restriction_problems(parts, [x['text'] for x in lines], allergens, diet)  # the finished lines must still pass the allergen and diet gate
    if bad:
        return dict(out, status='restriction_failed', failure=bad)
    return dict(out, status='ok', title=rewrite.title, steps=[L.STEP_NUMBER.sub('', s.text) for s in rewrite.instructions], new_lines=final)
