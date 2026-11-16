from unittest.mock import Mock
import json
from recipe_wrangler.repositories.matching_store import persist_profile_record


def test_composition_loading_uses_bounded_keyset_pages(monkeypatch):
    from unittest.mock import MagicMock
    from recipe_wrangler.repositories import matching_store as store
    connection = MagicMock()
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    pages = [[{'collection': 'a', 'candidate_id': '1'}],
             [{'collection': 'b', 'candidate_id': '2'}], []]
    def execute(statement, params=None):
        result = MagicMock()
        if params is not None:
            assert 'LIMIT 100' in str(statement)
            result.mappings.return_value = pages.pop(0)
        return result
    connection.execute.side_effect = execute
    monkeypatch.setattr(store, 'get_engine', lambda: engine)
    assert store.compositions('release') == [{'collection': 'a', 'candidate_id': '1'},
                                             {'collection': 'b', 'candidate_id': '2'}]
    assert connection.execute.call_args.args[1] == {'id': 'release', 'collection': 'b', 'candidate': '2'}


def test_saved_analysis_stages_run_and_snapshots_predecessor_without_activation():
    connection=Mock()
    previous={'recipe_id':'r','nutrition_source':'eu','total_nutrients':{'energy_kcal':100}}
    connection.execute.return_value.scalar.return_value=previous
    result=persist_profile_record(connection,{'recipe_id':'r','nutrition_source':'eu',
        'total_nutrients':{'energy_kcal':90},'nutrition_profiling_details':[{
            'ingredient':'milk','weight_g':100,'catalogue_release':'release',
            'quantity_input_hash':'shared-hash','quantity_version':'shared-v1',
            'mechanism_version':'version','observed_facts':{'facets':{'fat_level':'low'}}}]})
    assert result
    sql=[str(call.args[0]) for call in connection.execute.call_args_list]
    assert sum('INSERT INTO public.matching_profile_runs' in q for q in sql)==2
    assert not any('matching_active_profiles' in q for q in sql)
    saved=json.loads(connection.execute.call_args_list[-1].args[1]['profile'])
    assert saved['quantity_input_hash']=='shared-hash'
    assert saved['quantity_version']=='shared-v1'


def test_reference_and_legacy_profiles_do_not_create_matching_runs():
    connection=Mock()
    assert persist_profile_record(connection,{'nutrition_source':'safefood_rcsi',
        'nutrition_profiling_details':[{'catalogue_release':'r','mechanism_version':'v'}]}) is None
    assert persist_profile_record(connection,{'nutrition_source':'eu',
        'nutrition_profiling_details':[{'ingredient':'milk'}]}) is None
    connection.execute.assert_not_called()


def test_variant_query_filters_released_source_before_any_shortlist_limit(monkeypatch):
    from unittest.mock import MagicMock
    from recipe_wrangler.repositories import matching_store as store
    connection = MagicMock()
    connection.execute.return_value.mappings.return_value = [{"candidate_id": "test"}]
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(store, "get_engine", lambda: engine)
    assert store.variant_composition_records("release-1", "nutritional_ingredients_irish", dataset="source'quoted",
        source_release="1", identity="chicken", food_part="breast") == [{"candidate_id": "test"}]
    statement, params = connection.execute.call_args.args
    query = str(statement)
    assert "LIMIT" not in query.upper() and "r.status='ready'" in query
    assert "source'quoted" not in query and params["dataset"] == "source'quoted"
    assert params["regional"] == "nutritional_ingredients_irish" and params["part"] == "breast"
