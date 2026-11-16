"""LLM stages for small-gain adaptation, with a scripted fake model: no network, no cost."""
import json
from types import SimpleNamespace

import pytest

from recipe_wrangler.services.adaptation import small_gain_llm as L

LINES = [{'position': 0, 'text': '2 medium onions, diced', 'weight_g': 220.0},
         {'position': 1, 'text': '450g / 16 oz. loin of lamb, trimmed and diced', 'weight_g': 450.0},
         {'position': 2, 'text': '1 tablespoon of olive oil', 'weight_g': 13.8}]
STEPS = ['Heat the oil in a pan.', 'Add the lamb and brown well for 10 minutes.', 'Add the onions and 300g tinned tomatoes, then simmer the lamb for 90 minutes.']
RECIPE = {'instructions': STEPS}
SWAP = {'id': 1, 'summary': 'Replace lamb with turkey breast', 'flags': {'changes_dish_identity': True, 'cross_database': False},
        'changes': [{'position': 1, 'operation': 'SWAP_FOOD', 'category': 'protein_swap', 'source_line': LINES[1]['text'], 'matched_label': 'Lamb, leg, raw',
                     'candidate_label': 'Turkey breast skinless boneless raw', 'weight_before_g': 450.0, 'weight_after_g': 450.0}],
        'before_per_serving': {'saturated_fat_g': 2.8}, 'after_per_serving': {'saturated_fat_g': 0.9}, 'grade_before': 'Nutriscore_A', 'grade_after': 'Nutriscore_A', 'tier': 'small_gain', 'tradeoffs': []}
OIL = {'id': 2, 'summary': 'Use less olive oil', 'flags': {'changes_dish_identity': False, 'cross_database': False},
       'changes': [{'position': 2, 'operation': 'REDUCE_AMOUNT', 'category': 'oil', 'source_line': LINES[2]['text'], 'matched_label': 'Olive oil',
                    'weight_before_g': 13.8, 'weight_after_g': 6.9, 'reduction_fraction': 0.5}],
       'before_per_serving': {'saturated_fat_g': 2.8}, 'after_per_serving': {'saturated_fat_g': 2.5}, 'grade_before': 'Nutriscore_A', 'grade_after': 'Nutriscore_A', 'tier': 'small_gain', 'tradeoffs': []}
RESULT = {'recipe_id': 'r1', 'title': 'Lamb rogan josh', 'outcome': 'adapted', 'candidate_pack': [SWAP, OIL]}

GOOD = {'title': 'Turkey rogan josh',
        'instructions': [{'source_steps': [1], 'text': 'Heat the oil in a pan.'}, {'source_steps': [2], 'text': 'Add the turkey and brown well for 10 minutes.'},
                         {'source_steps': [3], 'text': 'Add the onions and 300g tinned tomatoes, then simmer the turkey for 90 minutes.'}],
        'ingredient_lines': [{'position': 1, 'new': '450g / 16 oz. turkey breast, trimmed and diced'}],
        'notes': []}


def scripted(*answers):
    seen = []

    def llm(messages, schema, max_tokens):
        seen.append((schema, messages))
        return json.dumps(answers[len(seen) - 1])
    llm.seen = seen
    return llm


def test_good_run_returns_title_instructions_and_lines():
    llm = scripted({'choice': 1, 'ranking': [1, 2], 'reason': 'turkey keeps the curry'}, GOOD)
    out = L.run_llm_stages(RESULT, RECIPE, LINES, llm)
    assert out['status'] == 'ok' and out['calls'] == 2 and out['title'] == 'Turkey rogan josh' and out['title_changed']
    assert len(out['instructions']) == 3 and out['notes'][0]['kind'] == 'method' and 'leaner than lamb' in out['notes'][0]['text'] and out['unsaved']
    assert 'per_serving_after' in llm.seen[0][1][1]['content']   # the model sees the outcome table for each candidate


def test_none_is_a_valid_choice():
    out = L.run_llm_stages(RESULT, RECIPE, LINES, scripted({'choice': None, 'ranking': [], 'reason': 'neither suits a curry'}))
    assert out['status'] == 'no_culinary_candidate' and out['calls'] == 1


def test_unknown_choice_id_is_rejected_then_retried_once():
    llm = scripted({'choice': 9, 'ranking': [], 'reason': 'x y z'}, {'choice': 2, 'ranking': [2], 'reason': 'just less oil'},
                   {'title': 'Lamb rogan josh', 'instructions': [{'source_steps': [i], 'text': t} for i, t in enumerate(STEPS, 1)],
                    'ingredient_lines': [{'position': 2, 'new': '½ tablespoon of olive oil'}], 'notes': []})
    out = L.run_llm_stages(RESULT, RECIPE, LINES, llm)
    assert out['status'] == 'ok' and out['calls'] == 3 and out['candidate']['id'] == 2 and not out['title_changed']
    assert 'rejected' in llm.seen[1][1][1]['content']


@pytest.mark.parametrize('mutate,needle', [
    (lambda r: r.update(title='Lamb rogan josh'), 'title must replace'),
    (lambda r: r['instructions'][2].update(text='Add the onions and 300g tinned tomatoes, then simmer the lamb for 90 minutes.'), 'still mentioned'),
    (lambda r: r['instructions'][2].update(text='Add the onions and 300g tinned tomatoes, then simmer the turkey for 90 minutes and add 2 tbsp cream.'), 'invented quantities'),
    (lambda r: r['instructions'].pop(), 'not covered'),
    (lambda r: r['instructions'][0].update(text='Heat the olive oil in a pan.'), 'unrelated to the chosen change'),
    (lambda r: r['instructions'][1].update(text='Add the turkey and brown for 10 minutes.'), 'unrelated to the chosen change'),
    (lambda r: r['instructions'].__setitem__(slice(0, 2), [{'source_steps': [1, 2], 'text': 'Heat the oil in a pan. Add the turkey and brown well for 10 minutes.'}]), 'one output step per input step'),
    (lambda r: r['ingredient_lines'][0].update(new='700g turkey breast'), 'must keep the original quantities'),
    (lambda r: r.update(ingredient_lines=[]), 'is missing'),
])
def test_validator_rejects_bad_rewrites(mutate, needle):
    bad = json.loads(json.dumps(GOOD))
    mutate(bad)
    problems = L.validate_rewrite(L.RewriteOut.model_validate(bad), 'Lamb rogan josh', STEPS, SWAP, L.candidate_lines(SWAP, LINES))
    assert any(needle in p for p in problems), problems


def test_rewrite_that_stays_bad_after_the_retry_fails_with_a_fallback_title():
    bad = json.loads(json.dumps(GOOD)); bad['title'] = 'Lamb rogan josh'
    out = L.run_llm_stages(RESULT, RECIPE, LINES, scripted({'choice': 1, 'ranking': [1], 'reason': 'turkey fits'}, bad, bad))
    assert out['status'] == 'rewrite_failed' and out['calls'] == 3 and out['fallback_title'] == 'Turkey rogan josh'


def test_title_must_not_change_for_an_amount_cut():
    out = L.RewriteOut.model_validate({'title': 'Light lamb rogan josh', 'instructions': [{'source_steps': [1, 2, 3], 'text': 'Cook it all for 10 minutes.'}],
                                       'ingredient_lines': [{'position': 2, 'new': '½ tablespoon of olive oil'}], 'notes': []})
    assert any('title must stay unchanged' in p for p in L.validate_rewrite(out, 'Lamb rogan josh', STEPS, OIL, L.candidate_lines(OIL, LINES)))


def test_quantities_normalise_units_and_ignore_words():
    assert L.quantities('300g and 1,5 hours at 180°C, 2 garlic cloves, 1 large onion') == {('300', 'g'), ('1.5', 'h'), ('180', 'deg')}


def test_candidate_lines_give_deterministic_new_text():
    assert L.candidate_lines(OIL, LINES)[0]['suggested_new'] == '½ tablespoon of olive oil'
    assert L.candidate_lines(SWAP, LINES)[0]['swap'] is True


# --- the real client: hosted OpenRouter only, durable cap, every attempt reserved ---
class FakeClient:
    def __init__(self, finish='stop'):
        self.finish = finish
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        message = SimpleNamespace(content='{"ok": true}')
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=self.finish)], usage=None)

    def __enter__(self): return self
    def __exit__(self, *a): return False


def client(tmp_path, **kw):
    fake = FakeClient(kw.pop('finish', 'stop'))
    return L.CappedOpenRouter(tmp_path / 'ledger.json', kw.pop('cap', 1), kw.pop('attempts', 5), client_factory=lambda: fake), fake


def test_client_refuses_local_models(tmp_path, monkeypatch):
    monkeypatch.setenv('ADAPT_LLM_SOURCE', 'vllm')
    c, fake = client(tmp_path)
    with pytest.raises(L.BudgetExceeded, match='local models'):
        c([{'role': 'user', 'content': 'x'}], 'ChoiceOut', 1000)
    assert fake.calls == 0


def test_client_reserves_each_attempt_and_stops_at_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv('ADAPT_LLM_SOURCE', 'openrouter'); monkeypatch.setenv('OPENROUTER_API_KEY', 'test-key')
    c, fake = client(tmp_path, cap='0.004', attempts=10)          # each 3000-token call reserves about $0.0018
    msg = [{'role': 'user', 'content': 'hello'}]
    c(msg, 'ChoiceOut', 3000); c(msg, 'ChoiceOut', 3000)
    with pytest.raises(L.BudgetExceeded, match='exhausted'):
        c(msg, 'ChoiceOut', 3000)
    ledger = json.loads((tmp_path / 'ledger.json').read_text())
    assert len(ledger['attempts']) == 2 and fake.calls == 2 and all(a['status'] == 'received' for a in ledger['attempts'])


def test_client_keeps_the_reservation_of_a_failed_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv('ADAPT_LLM_SOURCE', 'openrouter'); monkeypatch.setenv('OPENROUTER_API_KEY', 'test-key')
    c, fake = client(tmp_path, finish='length', attempts=1)
    with pytest.raises(ValueError, match='incomplete'):
        c([{'role': 'user', 'content': 'hello'}], 'ChoiceOut', 1000)
    assert json.loads((tmp_path / 'ledger.json').read_text())['attempts'][0]['reserved_usd']
    with pytest.raises(L.BudgetExceeded):
        c([{'role': 'user', 'content': 'hello'}], 'ChoiceOut', 1000)      # the one allowed attempt is spent


def test_a_bundle_that_changes_two_lines_validates_each_line():
    both = {'id': 3, 'summary': 'swap lamb and halve the oil', 'flags': {'changes_dish_identity': True, 'cross_database': False},
            'changes': SWAP['changes'] + OIL['changes'], 'before_per_serving': {}, 'after_per_serving': {}, 'grade_before': 'Nutriscore_A', 'grade_after': 'Nutriscore_A', 'tier': 'small_gain', 'tradeoffs': []}
    out = json.loads(json.dumps(GOOD)); out['ingredient_lines'].append({'position': 2, 'new': '½ tablespoon of olive oil'})
    assert L.validate_rewrite(L.RewriteOut.model_validate(out), 'Lamb rogan josh', STEPS, both, L.candidate_lines(both, LINES)) == []
    out['ingredient_lines'][1]['new'] = '2 tablespoons of olive oil'
    assert any('reduced quantities' in p for p in L.validate_rewrite(L.RewriteOut.model_validate(out), 'Lamb rogan josh', STEPS, both, L.candidate_lines(both, LINES)))


def test_swap_note_is_written_in_code_and_only_for_a_lean_poultry_swap():
    assert 'Turkey is leaner than lamb' in L.swap_notes(SWAP)[0]['text'] and L.swap_notes(OIL) == []


def test_unexplained_edits_ignore_punctuation_and_articles_only():
    allowed = {'lamb', 'turkey'}
    assert L.unexplained_edits('Add the lamb, then stir.', 'Add turkey then stir', allowed) == []
    assert L.unexplained_edits('Cook until carmelized.', 'Cook until caramelized.', allowed) == ['carmelized', 'caramelized']


def test_database_style_line_keeps_the_database_name_for_the_profiler():
    assert L.display_line('80 g Celery, raw', '120 g celery', '120 g Celery, raw', False, None) == '120 g Celery, raw'
    assert L.display_line('60 g Pasta, white, dried, raw', 'x', 'use y', True, 'Pasta, wholewheat, spaghetti, dried, raw') == '60 g Pasta, wholewheat, spaghetti, dried, raw'
    assert L.display_line('450g / 1lb of boneless pork', '450g / 1lb turkey breast', 'use x', True, 'Turkey breast skinless boneless raw', 'Pork, loin, raw') == '450g / 1lb turkey breast'
    assert L.display_line('2 tablespoons of vegetable oil', '1 tablespoon of vegetable oil', '1 tablespoon of vegetable oil', False, None) == '1 tablespoon of vegetable oil'


def test_plural_fix_keeps_mixed_numbers():
    assert L.display_line('3 tablespoons of olive oil', '1 ½ tablespoons of olive oil', '', False, None) == '1 ½ tablespoons of olive oil'
    assert L.display_line('1 tablespoons of olive oil', '½ tablespoons of olive oil', '', False, None) == '½ tablespoon of olive oil'


def test_wholemeal_swap_keeps_the_pasta_shape():
    lines = [{'position': 0, 'text': '200g penne pasta', 'weight_g': 200.0}]
    steps = ['Cook the penne pasta for 10 minutes.']
    cand = {'id': 1, 'summary': 'wholewheat', 'flags': {'changes_dish_identity': False, 'cross_database': False},
            'changes': [{'position': 0, 'operation': 'SWAP_FOOD', 'category': 'wholemeal_pasta', 'source_line': '200g penne pasta',
                         'matched_label': 'Pasta, white, dried, raw', 'candidate_label': 'Pasta, wholewheat, spaghetti, dried, raw'}]}
    def out(line, step):
        return L.RewriteOut.model_validate({'title': 'Pasta', 'instructions': [{'source_steps': [1], 'text': step}], 'ingredient_lines': [{'position': 0, 'new': line}], 'notes': []})
    bad = L.validate_rewrite(out('200g wholewheat spaghetti', 'Cook the wholewheat spaghetti for 10 minutes.'), 'Pasta', steps, cand, L.candidate_lines(cand, lines))
    assert any('pasta shape' in p for p in bad)
    good = L.validate_rewrite(out('200g wholewheat penne pasta', 'Cook the wholewheat penne pasta for 10 minutes.'), 'Pasta', steps, cand, L.candidate_lines(cand, lines))
    assert not any('pasta shape' in p for p in good), good


def test_unrelated_step_edit_is_restored_not_failed():
    out = json.loads(json.dumps(GOOD)); out['instructions'][0]['text'] = 'Heat the olive oil in a big pan.'
    parsed = L.RewriteOut.model_validate(out)
    L.restore_unrelated_steps(parsed, STEPS, SWAP, L.candidate_lines(SWAP, LINES))
    assert parsed.instructions[0].text == STEPS[0] and parsed.instructions[1].text == GOOD['instructions'][1]['text']


def test_mislabelled_line_position_is_reassigned_by_its_words():
    out = L.RewriteOut.model_validate({'title': 'Lamb rogan josh', 'instructions': [{'source_steps': [i], 'text': t} for i, t in enumerate(STEPS, 1)],
                                       'ingredient_lines': [{'position': 0, 'new': '½ tablespoon of olive oil'}], 'notes': []})
    changed = L.candidate_lines(OIL, LINES)
    L.restore_unrelated_steps(out, STEPS, OIL, changed)
    assert out.ingredient_lines[0].position == 2 and L.validate_rewrite(out, 'Lamb rogan josh', STEPS, OIL, changed) == []


def test_fish_for_fish_swap_renames_the_dish_and_uses_the_food_word():
    cand = {'changes': [{'operation': 'SWAP_FOOD', 'category': 'cost_swap', 'source_line': '400g salmon fillets',
                         'matched_label': 'salmon fillet', 'candidate_label': 'Brook trout'}]}
    assert L.swap_words(cand) == [('salmon', 'trout')]
    assert L.swap_words({'changes': [{'operation': 'SWAP_FOOD', 'source_line': '200g pasta', 'matched_label': 'Pasta, white', 'candidate_label': 'Pasta, wholewheat'}]}) == []


TIME_STEPS = ['Roast the potatoes for 20 minutes.', 'Dice the tomatoes.', 'Add the tomatoes to the potatoes.']
TIME_OVERLAP = dict(anchor_step=1, work_step=2, anchor_quote='Roast the potatoes for 20 minutes.', window_minutes=20.0,
                    code_step='While the potatoes roast, dice the tomatoes.')


def _fake(*answers):
    replies = list(answers)
    return lambda messages, schema, max_tokens: json.dumps(replies.pop(0))


def test_time_tip_minutes_come_from_code_capped_by_the_window():
    out = L.run_time_tip('Roast', TIME_STEPS, TIME_OVERLAP, _fake(dict(step_text='While the potatoes roast, dice the tomatoes.',
        prep_minutes_low=5, prep_minutes_high=30, tip='Dice the tomatoes while the potatoes roast, saving {minutes}.')))
    assert out['status'] == 'ok' and out['minutes_saved']['low'] == 5 and out['minutes_saved']['high'] == 20
    assert out['tip'] == 'Dice the tomatoes while the potatoes roast, saving 5-20 minutes.'


@pytest.mark.parametrize('bad', [
    dict(step_text='While the potatoes roast, cut the tomatoes.'),                 # step reworded
    dict(step_text='While the potatoes roast for 20 minutes, dice the tomatoes.'),  # number in the lead-in
    dict(step_text='Now dice the tomatoes.'),                                      # no 'while'
    dict(tip='Saves 10 minutes.'),                                                  # number written by the model
])
def test_time_tip_rejects_rewording_numbers_and_missing_parallel_lead_in(bad):
    good = dict(step_text='While the potatoes roast, dice the tomatoes.', prep_minutes_low=5, prep_minutes_high=8,
                tip='Dice the tomatoes while the potatoes roast, saving {minutes}.')
    out = L.run_time_tip('Roast', TIME_STEPS, TIME_OVERLAP, _fake({**good, **bad}, {**good, **bad}))
    assert out['status'] == 'time_tip_failed' and out['calls'] == 2 and out['fallback_step'] == TIME_OVERLAP['code_step']


COST = {'summary': 'Replace salmon with trout fillet, same weight; cost', 'tier': 'cost', 'grade_before': 'Nutriscore_A', 'grade_after': 'Nutriscore_A',
        'changes': [{'matched_label': 'salmon fillet', 'candidate_label': 'trout fillet'}],
        'before_per_serving': {'cost_eur': 3.80}, 'after_per_serving': {'cost_eur': 1.91}}
CO2 = dict(COST, tier='co2', before_per_serving={'co2_kg': 4.14}, after_per_serving={'co2_kg': 0.47})
NUTRITION = dict(COST, tier='better_letter', grade_before='Nutriscore_B', grade_after='Nutriscore_A',
                 before_per_serving={'saturated_fat_g': 4.0, 'sugar_g': 2.0, 'energy_kcal': 400},
                 after_per_serving={'saturated_fat_g': 2.8, 'sugar_g': 2.0, 'energy_kcal': 380})


@pytest.mark.parametrize('candidate,facet,text', [
    (COST, 'cost', 'Trout works just as well and it saves €1.89 per serving.'),
    (CO2, 'sustainability', 'Trout works just as well and it cuts 3.67 kg CO2e per serving (89% less).'),
    (NUTRITION, 'nutrition', 'Trout works just as well and the Nutri-Score improves from B to A and it has 30% less saturated fat per serving.'),
])
def test_benefit_block_fills_the_measured_gain_into_the_free_text_tip(candidate, facet, text):
    block = L.benefit_block(candidate, 'Trout works just as well and {benefit}.', [])
    assert block['facet'] == facet and block['tip'] == text and block['tip_source'] == 'llm'
    assert block['swap'] == [{'from': 'salmon', 'to': 'trout', 'from_record': 'salmon fillet', 'to_record': 'trout fillet'}]


def test_benefit_block_falls_back_to_a_code_sentence_without_a_model_tip():
    block = L.benefit_block(COST, '', [])
    assert block['tip_source'] == 'code' and block['tip'] == 'Swap the salmon for trout: it saves €1.89 per serving.'


def test_time_tip_exports_the_same_benefit_block():
    out = L.run_time_tip('Roast', TIME_STEPS, TIME_OVERLAP, _fake(dict(step_text='While the potatoes roast, dice the tomatoes.',
        prep_minutes_low=5, prep_minutes_high=8, tip='Dice the tomatoes while the potatoes roast, saving {minutes}.')))
    assert out['benefit']['facet'] == 'time' and out['benefit']['metric']['low'] == 5 and '5-8 minutes' in out['benefit']['tip']
