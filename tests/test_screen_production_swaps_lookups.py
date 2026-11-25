from types import SimpleNamespace

from recipe_wrangler.services.adaptation import nutrition_adaptation as na
from recipe_wrangler.services.adaptation import nutritional_variants, production_candidates


def _item(position):
    composition = SimpleNamespace(values_per_100g={m: 1.0 for m in na.TARGETS}, provenance={},
                                  ref=SimpleNamespace(candidate_id=str(position)))
    return SimpleNamespace(position=position, name=f'food {position}', original_text=f'food {position}',
                           quantity=SimpleNamespace(weight_g=100.0 + position), composition=composition)


def test_position_lookups_run_once_across_metrics(monkeypatch):
    calls = {'graph': [], 'part': []}
    monkeypatch.setattr(na, 'production_score_context', lambda snapshot, profile: object())
    monkeypatch.setattr(nutritional_variants, 'nutritional_variant_records', lambda *a: [])
    monkeypatch.setattr(production_candidates, 'production_candidate_records',
                        lambda snapshot, position: calls['graph'].append(position) or ([], []))
    monkeypatch.setattr(production_candidates, 'food_part_candidate_records',
                        lambda snapshot, position: calls['part'].append(position) or ([], []))
    snapshot = SimpleNamespace(ingredients=[_item(p) for p in range(5)])

    na.screen_production_swaps(snapshot, profile={})

    # Every metric ranks the same ingredients first; each position is still looked up once.
    assert sorted(calls['graph']) == sorted(set(calls['graph'])) and len(calls['graph']) == na.GRAPH_POSITIONS_PER_METRIC
    assert sorted(calls['part']) == list(range(5))
