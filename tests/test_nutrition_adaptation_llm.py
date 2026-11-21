"""LLM stages for nutrition adaptation, with a scripted fake model: no network, no cost."""
import json
from types import SimpleNamespace

import pytest

from recipe_wrangler.services.adaptation import nutrition_adaptation_llm as L

LINES = [{'position': 0, 'text': '2 medium onions, diced', 'weight_g': 220.0},
         {'position': 1, 'text': '450g / 16 oz. loin of lamb, trimmed and diced', 'weight_g': 450.0},
         {'position': 2, 'text': '1 tablespoon of olive oil', 'weight_g': 13.8}]
STEPS = ['Heat the oil in a pan.', 'Add the lamb and brown well for 10 minutes.', 'Add the onions and 300g tinned tomatoes, then simmer the lamb for 90 minutes.']
RECIPE = {'instructions': STEPS}
SWAP = {'id': 1, 'summary': 'Replace lamb with turkey breast', 'flags': {'changes_dish_identity': True, 'cross_database': False},
        'changes': [{'position': 1, 'operation': 'SWAP_FOOD', 'category': 'protein_swap', 'source_line': LINES[1]['text'], 'matched_label': 'Lamb, leg, raw',
                     'candidate_label': 'Turkey breast skinless boneless raw', 'weight_before_g': 450.0, 'weight_after_g': 450.0}],
        'before_per_serving': {'saturated_fat_g': 2.8}, 'after_per_serving': {'saturated_fat_g': 0.9}, 'grade_before': 'Nutriscore_A', 'grade_after': 'Nutriscore_A', 'tier': 'nutrition_adaptation', 'tradeoffs': []}
OIL = {'id': 2, 'summary': 'Use less olive oil', 'flags': {'changes_dish_identity': False, 'cross_database': False},
       'changes': [{'position': 2, 'operation': 'REDUCE_AMOUNT', 'category': 'oil', 'source_line': LINES[2]['text'], 'matched_label': 'Olive oil',
                    'weight_before_g': 13.8, 'weight_after_g': 6.9, 'reduction_fraction': 0.5}],
       'before_per_serving': {'saturated_fat_g': 2.8}, 'after_per_serving': {'saturated_fat_g': 2.5}, 'grade_before': 'Nutriscore_A', 'grade_after': 'Nutriscore_A', 'tier': 'nutrition_adaptation', 'tradeoffs': []}
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
            'changes': SWAP['changes'] + OIL['changes'], 'before_per_serving': {}, 'after_per_serving': {}, 'grade_before': 'Nutriscore_A', 'grade_after': 'Nutriscore_A', 'tier': 'nutrition_adaptation', 'tradeoffs': []}
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
    (NUTRITION, 'nutrition', 'Trout works just as well and the Nutri-Score improves from B to A and the dish has 30% less saturated fat per serving.'),
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


def test_scaled_swap_states_the_new_amount():
    salt = {'operation': 'SWAP_FOOD', 'category': 'low_sodium_salt', 'position': 0, 'source_line': '15g Salt', 'matched_label': 'Salt',
            'candidate_label': 'Salt low sodium LoSalt', 'weight_before_g': 15.0, 'weight_after_g': 11.25}
    out = L.candidate_lines({'changes': [salt]}, [{'position': 0, 'text': '15g Salt', 'weight_g': 15.0}])
    assert out[0]['scaled'] and L.quantities(out[0]['suggested_new']) == {('11', 'g')} or L.quantities(out[0]['suggested_new']) == {('11.25', 'g')}


def test_database_style_salt_line_shows_the_new_amount():
    line = L.display_line('15g Salt, white (sea, igneous or rock)', 'x', '11g low-sodium salt, about a quarter less', True,
                          'Salt low sodium LoSalt', 'Salt, white (sea, igneous or rock)', scaled=True)
    assert line.startswith('11g ') and 'LoSalt' in line


def test_a_cut_ingredient_may_not_keep_its_old_amount_in_a_step():
    changed = [{'position': 0, 'old': '30ml of olive oil', 'suggested_new': '22ml of olive oil', 'swap': False, 'matched': 'Oil, olive'}]
    bad = L.stale_quantity_problems(['Sprinkle over 20ml of olive oil.', 'Heat 10 ml of olive oil in a pan.'], changed)
    assert len(bad) == 2 and 'step 1' in bad[0]
    assert L.stale_quantity_problems(['Sprinkle over the olive oil.', 'Heat 22ml of olive oil in a pan.', 'Cook 20 g of pasta.'], changed) == []


def test_replaced_cheese_does_not_survive_as_a_generic_word():
    cand = {'changes': [{'position': 3, 'operation': 'SWAP_FOOD', 'matched_label': 'CHEESE EDAMER', 'source_line': '300g CHEESE "EDAMER"', 'candidate_label': 'Yoghurt low fat'}]}
    lines = [{'position': 3, 'text': '300g CHEESE "EDAMER"'}, {'position': 4, 'text': '200g pasta'}]
    assert L.role_word_problems(['Layer the pasta, bechamel and grated cheese.'], cand, lines)
    assert not L.role_word_problems(['Layer the pasta, bechamel and the yoghurt.'], cand, lines)
    assert not L.role_word_problems(['Top with grated cheese.'], cand, lines + [{'position': 5, 'text': '30g parmesan cheese'}])


def test_code_removes_the_old_amount_from_a_step():
    changed = [{'position': 0, 'old': '30ml of olive oil', 'suggested_new': '22ml of olive oil', 'swap': False, 'matched': 'Oil, olive'}]
    out = L.RewriteOut(title='Test dish', instructions=[L.StepOut(text='Sprinkle over 20ml of olive oil.', source_steps=[1]), L.StepOut(text='Heat 2 teaspoons of the oil and roast.', source_steps=[2]),
                                                  L.StepOut(text='Cook 20 g of pasta with 22ml of olive oil.', source_steps=[3])], ingredient_lines=[], tip='', notes=[])
    L.fix_stale_quantities(out, changed)
    assert [s.text for s in out.instructions] == ['Sprinkle over the olive oil.', 'Heat the oil and roast.', 'Cook 20 g of pasta with 22ml of olive oil.']
    assert L.stale_quantity_problems([s.text for s in out.instructions], changed) == []


def test_removing_an_old_amount_from_a_step_is_an_explained_edit():
    changed = [{'position': 0, 'old': '30ml of olive oil', 'suggested_new': '22ml of olive oil', 'swap': False, 'matched': 'Oil, olive'}]
    allowed = L.stale_quantity_tokens(['Sprinkle over 20ml of olive oil.', 'Heat 10 ml of olive oil.'], changed)
    assert not L.unexplained_edits('Sprinkle over 20ml of olive oil.', 'Sprinkle over the olive oil.', allowed)
    assert L.unexplained_edits('Sprinkle over 20ml of olive oil.', 'Sprinkle over the lovely olive oil.', allowed)


def test_half_wholemeal_flour_keeps_the_total_amount():
    flour = {'operation': 'SWAP_FOOD', 'category': 'half_wholemeal_flour', 'position': 0, 'source_line': '850g plain flour', 'matched_label': 'Flour, wheat, white, plain',
             'candidate_label': 'Flour, wheat, wholemeal', 'weight_before_g': 850.0, 'weight_after_g': 425.0}
    out = L.candidate_lines({'changes': [flour]}, [{'position': 0, 'text': '850g plain flour', 'weight_g': 850.0}])
    assert not out[0]['scaled']  # so the validator keeps the original 850 g
    assert L.display_line('850g plain flour', '850g half white, half wholemeal flour', out[0]['suggested_new'], True, 'Flour, wheat, wholemeal', 'Flour, wheat, white, plain', False) == '850g half white, half wholemeal flour'
    assert L.display_line('50 g Flour, wheat, white, plain, soft', 'x', out[0]['suggested_new'], True, 'Flour, wheat, wholemeal', 'Flour, wheat, white, plain, soft', False).startswith('50 g ')
    from recipe_wrangler.services.adaptation import nutrition_adaptation as sg
    assert 'keeping the total amount' in sg.describe_change(flour)


def test_mixed_fractions_are_one_quantity():
    assert L.quantities('around 2kg / 4½ lb') == {('2', 'kg'), ('4.5', 'lb')}
    assert L.quantities('½ lb') == {('0.5', 'lb')}


def test_new_food_that_contains_the_old_word_is_not_a_leftover():
    cand = {'changes': [{'operation': 'SWAP_FOOD', 'category': 'similar_part', 'matched_label': 'Ham, cooked', 'source_line': '200g cooked ham',
                         'candidate_label': 'Pork ham roasted'}]}
    assert L.swap_words(cand) == []


def test_lines_are_mapped_by_order_when_no_word_is_shared():
    changed = [{'position': 2, 'old': '51g Lard', 'suggested_new': 'use oil', 'swap': True, 'label': 'Oil, canola', 'matched': 'Lard'}]
    cand = {'changes': [{'position': 2, 'operation': 'SWAP_FOOD', 'matched_label': 'Lard', 'candidate_label': 'Oil, canola'}]}
    out = L.RewriteOut(title='Fried potatoes', instructions=[L.StepOut(text='Fry in the lard.', source_steps=[1])],
                       ingredient_lines=[L.LineOut(position=1, new='41g canola oil')], tip='', notes=[])
    L.restore_unrelated_steps(out, ['Fry in the lard.'], cand, changed)
    assert out.ingredient_lines[0].position == 2


def test_old_meat_word_in_a_step_is_replaced_by_code():
    cand = {'changes': [{'operation': 'SWAP_FOOD', 'category': 'protein_swap', 'matched_label': 'Lamb leg raw', 'source_line': '1 leg of lamb', 'candidate_label': 'Turkey breast skinless'}]}
    out = L.RewriteOut(title='Roast turkey', instructions=[L.StepOut(text='When the lamb is cooked remove it. Lamb rests well.', source_steps=[1])], ingredient_lines=[], tip='', notes=[])
    L.fix_leftover_swap_words(out, cand)
    assert out.instructions[0].text == 'When the turkey is cooked remove it. Turkey rests well.'


def test_half_and_half_flour_may_show_the_two_halves():
    assert L.half_quantities({('850', 'g')}) == {('425', 'g')} and L.half_quantities({('5', 'g')}) == {('2.5', 'g')}
    assert L.half_amount('50 g ') == '25 g ' and L.half_amount('5 g ') == '2.5 g '
    line = L.display_line('50 g Flour, wheat, white, plain, soft', 'x', 'use half', True, 'Flour, wheat, wholemeal', 'Flour, wheat, white, plain, soft', False, True)
    assert line == '25 g Flour, wheat, white, plain, soft and 25 g Flour, wheat, wholemeal'


def test_terse_use_lines_keep_the_original_count():
    stock = {'operation': 'SWAP_FOOD', 'category': 'reduced_salt_stock', 'position': 0, 'source_line': '½ vegetable stock cube', 'matched_label': 'Stock cubes, vegetable',
             'candidate_label': 'Stock cube, vegetable, reduced salt', 'weight_before_g': 5.0, 'weight_after_g': 5.0}
    out = L.candidate_lines({'changes': [stock]}, [{'position': 0, 'text': '½ vegetable stock cube', 'weight_g': 5.0}])
    assert out[0]['suggested_new'].startswith('½ ') and 'use ' not in out[0]['suggested_new']
    assert L.display_line('½ vegetable stock cube', 'use reduced-salt stock', 'use reduced-salt stock', True, 'x', 'y') == '½ reduced-salt stock cube'
    assert L.display_line('Strawberry jam for filling', 'use reduced-sugar jam', 'use reduced-sugar jam', True, 'x', 'y') == 'reduced-sugar jam'
    # the validator refuses a model line that drops the count
    changed = out
    step = L.StepOut(text='Add the stock.', source_steps=[1])
    bad = L.RewriteOut(title='Pea soup', instructions=[step], ingredient_lines=[L.LineOut(position=0, new='reduced-salt stock cube')], tip='', notes=[])
    cand = {'changes': [stock], 'summary': 's'}
    assert any('original count' in p for p in L.validate_rewrite(bad, 'Pea soup', ['Add the stock.'], cand, changed))


def test_cookbook_flour_halves_are_stated_as_two_lines():
    from recipe_wrangler.services.adaptation.nutrition_adaptation_llm import display_line
    line = display_line('500g flour', '250g flour, half white, half wholemeal', '', True, 'half white, half wholemeal flour', 'Wheat flour, white', partial=True)
    assert line == '250g plain flour\n250g wholemeal flour'
    assert display_line('225g / 8 oz. of flour', 'x', '', True, 'l', 'Wheat flour, white', partial=True) == '112.5g plain flour\n112.5g wholemeal flour'


def test_wholemeal_swap_keeps_the_recipe_pasta_shape():
    from types import SimpleNamespace as N
    from recipe_wrangler.services.adaptation.nutrition_adaptation_llm import fix_pasta_shape
    out = N(ingredient_lines=[N(new='60g / 2oz. of wholewheat spaghetti')], instructions=[N(text='Cook the pasta.')])
    fix_pasta_shape(out, ['Cook the pasta.'], [{'old': '60g / 2oz. of penne pasta'}])
    assert out.ingredient_lines[0].new == '60g / 2oz. of wholewheat penne'


def test_tin_swapped_for_cooked_pulses_states_the_weight():
    from recipe_wrangler.services.adaptation.nutrition_adaptation_llm import display_line
    got = display_line('1 small tin of chickpeas (210g / 7oz.), drained', '1 small tin of chickpeas, boiled in unsalted water', '', True, 'x', 'Chick pea, canned, drained')
    assert got == '125g of boiled chickpeas'


@pytest.mark.parametrize('old, model, want', [
    ('2 small tins of chickpeas (420g / 15oz.), drained', '2 small tins of chickpeas, boiled in unsalted water', '250g of boiled chickpeas'),
    ('400g tin of chickpeas rinsed and drained', '400g boiled chickpeas', '240g of boiled chickpeas'),
    ('400 g tin chickpeas, drained', '400 g boiled chickpeas', '240g of boiled chickpeas'),
    ('400g / 14 oz. can chick peas, drained', '400g / 14 oz. boiled chickpeas', '240g of boiled chickpeas'),
    ('1 tin of drained chickpeas (400g / 14oz.)', '1 boiled chickpeas (400g / 14oz.)', '240g of boiled chickpeas'),
    ('1 small tin chickpeas (210g / 7.5oz), drained', '1 boiled chickpeas (210g)', '125g of boiled chickpeas'),
    ('2 x 400g cans of chickpeas, drained and rinsed', '2 x 400g boiled chickpeas', '480g of boiled chickpeas'),
    ('2 x 400g tins of chickpeas, drained and rinsed', '2 x 400g boiled chickpeas', '480g of boiled chickpeas'),
])
def test_tin_forms_swapped_for_cooked_pulses_state_the_drained_weight(old, model, want):
    from recipe_wrangler.services.adaptation.nutrition_adaptation_llm import display_line
    assert display_line(old, model, '', True, 'x', 'Chick pea, canned, drained') == want


@pytest.mark.parametrize('old, model, weight, want', [
    ('1 ham, around 2kg / 4½ lb', '1 lean or turkey ham, around 2kg / 4½ lb', 2000, '2kg of turkey ham'),
    ('1 boneless half leg of lamb, trimmed - around 1½ kg / 3½lb', '1 turkey breast, trimmed - around 1½ kg / 3½lb', 1000, '1000g of turkey breast, trimmed'),
    ('8 chicken thighs, skin and bone removed (or 4 chicken legs, skin removed)', 'chicken inner breast fillet', 1000, '1000g of chicken inner breast fillet'),
])
def test_swapped_meat_line_keeps_its_weight(old, model, weight, want):
    assert L.display_line(old, model, '', True, 'x', None, weight_g=weight) == want


def test_drained_weight_is_not_cut_again():
    assert L.display_line('1 x 240g / 8½ tin of kidney beans, drained weight', '240g red kidney beans, boiled', '', True, 'x', None) == '240g of red kidney beans, boiled'
    assert L.display_line('1 fully cooked smoked ham – around 1.5kg / 3½lb', '1 fully cooked ham from chicken – around 1.5kg / 3½lb', '', True, 'x', None) == '1.5kg of fully cooked ham from chicken'


def test_stated_gross_weight_gives_way_to_the_baseline_edible_weight():
    assert L.display_line('1 whole chicken with giblets removed, around 1½ kg / 3½ lb', '1 whole chicken, meat only, around 1½ kg / 3½ lb', '', True, 'x', None, weight_g=912) == '910g of whole chicken, meat only'
    assert L.display_line('1 ham, around 2kg / 4½ lb', '1 lean ham, around 2kg / 4½ lb', '', True, 'x', None, weight_g=2000) == '2kg of lean ham'


def test_wholegrain_swap_leads_with_fibre_not_a_small_calorie_drop():
    c = {'before_per_serving': {'energy_kcal': 500.0, 'fibre_g': 3.0, 'sugar_g': 2.0}, 'after_per_serving': {'energy_kcal': 470.0, 'fibre_g': 7.0, 'sugar_g': 2.0},
         'grade_before': 'Nutriscore_A', 'grade_after': 'Nutriscore_A', 'changes': [{'category': 'wholemeal_pasta'}]}
    m = L.benefit_metric(c)
    assert m['name'] == 'fibre_g' and 'more fibre' in m['text']
    assert L.benefit_metric(dict(c, changes=[{'category': 'canned_chickpeas_to_cooked'}]))['name'] == 'energy_kcal'


def test_cookbook_line_drops_database_only_qualifiers():
    assert L.cookbook_line('350g Chicken breast meat only raw') == '350g chicken breast'


def test_a_replaced_cut_must_not_survive_in_the_steps():
    lines = [{'position': 0, 'text': '4 pork chops'}]
    final = [SimpleNamespace(position=0, new='4 chicken breast fillets')]
    assert L.cut_word_problems(['Rub each chop with spice.'], lines, final)
    assert not L.cut_word_problems(['Rub each fillet with spice.'], lines, final)


def test_tip_and_line_wording_are_cookbook_style():
    assert L.tidy_tip('Wholewheat pasta keeps the dish just as hearty while it has 133% more fibre per serving.') == 'Wholewheat pasta: the dish has 133% more fibre per serving.'
    assert L.tidy_tip('Chicken keeps the patty just as hearty while the Nutri-Score improves from C to B and it has 42% less saturated fat per serving.') == 'Chicken: the Nutri-Score improves from C to B and the dish has 42% less saturated fat per serving.'
    assert L.display_line('1 tsp salt', '1 tsp Salt low sodium LoSalt', '', True, 'x', None) == '1 tsp low-sodium salt (LoSalt)'
    assert L.display_line('1 whole meal wrap', '1 wholemeal wrap/tortilla wheat', '', True, 'x', None) == '1 wholemeal wrap'


def test_light_meat_record_name_becomes_breast():
    assert L.cookbook_line('4 chicken, light meat') == '4 chicken breast'


def test_a_swap_between_different_cuts_states_the_baseline_weight_not_the_old_count():
    assert L.display_line('8 chicken thighs, skin and bone removed', '8 chicken inner breast fillet', '', True, 'x', None, weight_g=1000) == '1000g of chicken inner breast fillet'
    assert L.display_line('16 Chicken drumsticks', '16 turkey breast', '', True, 'x', None, weight_g=1520) == '1520g of turkey breast'
    assert L.display_line('2 chicken breasts', '2 turkey breasts', '', True, 'x', None, weight_g=300) == '2 turkey breasts'


def test_lean_or_turkey_ham_is_one_food():
    assert L.display_line('1 ham', '1 lean or turkey ham', '', True, 'x', None) == '1 turkey ham'


def test_a_dropped_imperial_amount_is_not_a_quantity_change():
    ok = json.loads(json.dumps(GOOD)); ok['ingredient_lines'][0]['new'] = '450g turkey breast, trimmed and diced'
    assert not any('original quantities' in p for p in L.validate_rewrite(L.RewriteOut.model_validate(ok), 'Lamb rogan josh', STEPS, SWAP, L.candidate_lines(SWAP, LINES)))


def test_a_database_record_name_is_not_cookbook_wording_for_ham():
    line = L.display_line('200g / 8 oz. of cooked ham, shredded', '200g pork ham roasted w/o gravy frozen product', '200g Pork ham roasted w/o gravy frozen product', True, 'Pork ham roasted w/o gravy frozen product', 'Cooked ham, choice')
    assert line == '200g cooked ham'


def test_a_rewrite_must_keep_per_person_on_the_amount():
    ok = json.loads(json.dumps(GOOD)); ok['ingredient_lines'][0]['new'] = '450g turkey breast, trimmed and diced'
    changed = L.candidate_lines(SWAP, LINES)
    changed[0]['old'] = '450g lamb shoulder per person'
    assert any('per person' in p for p in L.validate_rewrite(L.RewriteOut.model_validate(ok), 'Lamb rogan josh', STEPS, SWAP, changed))
