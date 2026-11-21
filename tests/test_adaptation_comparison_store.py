"""Production-shaped synthetic records; no services, models or writes required."""

from copy import deepcopy
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy.exc import OperationalError

from recipe_wrangler.repositories import matching_store
from recipe_wrangler.services.adaptation import comparison_store as adapter
from recipe_wrangler.services.adaptation.models import QuantityBasisDeclaration, RawPartEdit
from recipe_wrangler.tools.nutrition_completeness import EU, METRICS


def annotation(part):
    text = f"raw boneless skinless chicken {part}"
    values = {"base_identity": "chicken", "biological_source": "chicken", "food_part": part,
              "preparation_state": "raw", "skin_status": "absent", "bone_status": "absent",
              "processes": [], "preservation_methods": [], "colour": None}
    quotes = {"base_identity": "chicken", "biological_source": "chicken", "food_part": part,
              "preparation_state": "raw", "skin_status": "skinless", "bone_status": "boneless"}
    return {"input_text": text, "parsed_text": text, "facets": values, "qualifiers": {},
            "annotation_version": "synthetic-parser-1", "modifiers": [], "review_reasons": [],
            "evidence": [{"field": "main_ingredient" if field == "base_identity" else "facets." + field,
                          "quote": quote, "polarity": "asserted"} for field, quote in quotes.items()]}


def record(part):
    values = dict.fromkeys(METRICS, 0.0)
    values.update(energy_kcal=200.0 if part == "thigh" else 120.0, fibre_g=None)
    label = f"raw boneless skinless chicken {part}"
    source = {"dataset": "synthetic-table", "release": "1", "record_id": "source-" + part}
    facts = {"source_key": "eu", "native_id": "native-" + part, "label": label,
             "source": source, "text_annotation": annotation(part), "native_annotation": None,
             "basis": {"amount": 100, "unit": "g", "part": "edible_portion", "evidence": "native_reader"},
             "values_per_100g": values, "blockers": [], "policy_version": "synthetic-policy-1"}
    return {"release_id": "release-1", "collection": "nutritional_ingredients_eu",
            "candidate_id": "candidate-" + part, "native_id": "native-" + part,
            "candidate": {"id": "candidate-" + part, "document": label, "metadata": {}},
            "annotation": facts,
            "nutrient_row": {"id": "native-" + part, "nutrients": {
                EU[metric]: {"value": value, "unit": "kcal" if metric == "energy_kcal" else "mg" if metric == "sodium_mg" else "g"}
                for metric, value in values.items() if value is not None}},
            "source_record": {"source": source, "label": label},
            "source_profile": {"source": source, "label": label, "values_per_100g": values,
                               "native_values": {"energy_kcal": [{"value": values["energy_kcal"], "raw_unit": "Kilocalories/100 gram"}]}}}


@pytest.fixture
def database(monkeypatch):
    # Recovery has its own parser fixtures; these boundary tests isolate saved facts.
    monkeypatch.setattr(adapter, "recover_source_evidence", lambda *args, **kwargs: None)
    source, target = record("thigh"), record("breast")
    detail = {"position": 0, "ingredient": "chicken", "name": "chicken", "weight_g": 200.0,
              "original_weight_g": 250.0, "original_ingredient_text": "200g raw boneless skinless chicken thigh",
              "quantity_status": "explicit_source_mass", "quantity_input_hash": "quantity-1",
              "quantity_evidence": {"kind": "explicit_source_mass", "weight_g": 200.0,
                                    "source_line": "200g raw boneless skinless chicken thigh"},
              "observed_facts": annotation("thigh"), "selected_candidate_id": "candidate-thigh",
              "canonical_food_id": "native-thigh", "selected_composition_facts": deepcopy(source["annotation"]),
              "values_per_100g": deepcopy(source["annotation"]["values_per_100g"]), "outcome": "automatic_estimate",
              "catalogue_release": "release-1", "assumptions": [{"policy": "synthetic_assumption"}]}
    active = {"run_id": "run-1", "recipe_id": "recipe-1", "nutrition_source": "irish",
              "input_hash": "inputs-1", "release_id": "release-1", "mechanism_version": "synthetic-1",
              "profile": {"catalogue_release": "release-1", "source_key": "irish", "quantity_input_hash": "quantity-1",
                          "serves": 2.0, "details": [detail], "nutrition_quality": {"status": "estimated"}}}
    reads = Mock(return_value=active)
    def records(release, collection, *, candidate_id=None, native_id=None):
        return [row for row in (source, target) if row["release_id"] == release and row["collection"] == collection
                and (row["candidate_id"] == candidate_id if candidate_id is not None else row["native_id"] == native_id)]
    lookup = Mock(side_effect=records)
    monkeypatch.setattr(matching_store, "active_profile", reads)
    monkeypatch.setattr(matching_store, "composition_records", lookup)
    for name in ("latest_profile", "save_run", "insert_run", "persist_profile_record", "enqueue_projection", "compositions"):
        monkeypatch.setattr(matching_store, name, Mock(side_effect=AssertionError("Unexpected lookup or write: " + name)))
    edit = RawPartEdit(recipe_id="recipe-1", baseline_run_id="run-1", quantity_input_hash="quantity-1", position=0,
                       source_identity="chicken", target_identity="chicken", source_biological_source="chicken",
                       target_biological_source="chicken", source_food_part="thigh", target_food_part="breast",
                       from_composition={key: source[key] for key in ("release_id", "collection", "candidate_id")},
                       to_composition={key: target[key] for key in ("release_id", "collection", "candidate_id")})
    return active, source, target, edit, reads, lookup


def test_active_snapshot_preserves_original_evidence_and_uses_exact_records(database):
    active, source, target, edit, reads, lookup = database
    before = deepcopy(active)
    snapshot = adapter.load_active_snapshot("recipe-1", "irish")
    reads.assert_called_once_with("recipe-1", "irish", include_provenance=True)
    lookup.assert_called_once_with("release-1", "nutritional_ingredients_eu", candidate_id="candidate-thigh")
    item = snapshot.ingredients[0]
    assert item.position == 0 and item.quantity.weight_g == 200
    assert item.quantity.basis == "unknown"
    assert item.quantity.evidence.status == "stated"
    assert item.observed_properties["skin_status"].value == "skinless"
    assert item.observed_properties["bone_status"].value == "boneless"
    assert item.observed_properties["food_part"].evidence.status == "stated"
    assert item.observed_properties["processes"].evidence.status == "inferred"
    assert item.composition.values_per_100g["fibre_g"] is None
    assert item.composition.values_per_100g["sodium_mg"] == 0
    assert "carbs_g" not in item.composition.nutrient_definitions
    assert snapshot.provenance["active_run"]["profile"]["details"][0]["original_weight_g"] == 250
    snapshot.provenance["active_run"]["profile"]["details"][0]["assumptions"].clear()
    assert active == before


def native_carbohydrate_record():
    row = record('thigh')
    source = {'dataset': 'A native table', 'release': 'EFSA EU FCDB 2026', 'record_id': 'source-thigh'}
    for item in (row['annotation'], row['source_record'], row['source_profile']):
        item['source'] = deepcopy(source)
    row['source_profile']['file'] = 'data/EFSA_EU_FCDB_2026/composition_values.csv'
    row['source_profile']['native_values']['carbs_g'] = [
        {'value': 0.0, 'raw_value': '0', 'raw_unit': 'Gram/100 gram', 'source_csv_row': 20}]
    return row


def legacy_efsa_record():
    row = record('thigh')
    row['native_id'] = 'efsa_table:710'
    row['annotation']['native_id'] = row['native_id']
    source = {'dataset': 'efsa_table', 'release': 'frozen_legacy_inventory', 'record_id': row['native_id']}
    for item in (row['annotation'], row['source_record'], row['source_profile']):
        item['source'] = deepcopy(source)
    row['nutrient_row'].update(id=row['native_id'], source='efsa_table',
        source_url='https://www.efsa.europa.eu/en/microstrategy/food-composition-data-2026')
    row['nutrient_row']['nutrients'][EU['carbs_g']].update(source_code='EFSA:CHO:710_CHO_202')
    return row


def test_legacy_efsa_component_code_establishes_only_same_table_native_definition():
    row = legacy_efsa_record()
    before = deepcopy(row)
    composition = adapter._composition(row)
    assert composition.nutrient_definitions['carbs_g'] == 'efsa_table:frozen_legacy_inventory:native_component:CHO'
    evidence = composition.provenance['nutrient_definition_evidence']['carbs_g']
    assert evidence['method'] == 'retained_efsa_component_code'
    assert row == before


@pytest.mark.parametrize('issue', ['code', 'empty', 'conversion', 'origin', 'dataset', 'identity', 'unit', 'value', 'release'])
def test_legacy_carbohydrate_wrapper_alone_does_not_establish_definition(issue):
    row = legacy_efsa_record()
    nutrient = row['nutrient_row']['nutrients'][EU['carbs_g']]
    if issue == 'code': nutrient.pop('source_code')
    elif issue == 'empty': nutrient['source_code'] = 'EFSA::710'
    elif issue == 'conversion': nutrient['source_code'] += ':conversion'
    elif issue == 'origin': row['nutrient_row']['source_url'] = 'unknown'
    elif issue == 'dataset': row['nutrient_row']['source'] = 'another-table'
    elif issue == 'identity': row['annotation']['source']['record_id'] = 'another-record'
    elif issue == 'unit': nutrient['unit'] = 'mg'
    elif issue == 'value': nutrient['value'] = None
    elif issue == 'release': row['annotation']['source']['release'] = 'unknown'
    assert adapter._legacy_efsa_carbohydrate_definition(row['nutrient_row'], row['annotation']['source'], 0.0) is None


def test_native_carbohydrate_definition_survives_wrapper_without_becoming_by_difference():
    row = native_carbohydrate_record()
    before = deepcopy(row)
    composition = adapter._composition(row)
    assert composition.nutrient_definitions['carbs_g'] == 'A native table:EFSA EU FCDB 2026:native_component:carbohydrate'
    assert row == before


@pytest.mark.parametrize('issue', ['missing', 'malformed', 'conflict', 'unit', 'value', 'raw_value', 'row', 'file', 'release'])
def test_unproven_native_carbohydrate_definition_stays_unknown(issue):
    row = native_carbohydrate_record()
    profile = row['source_profile']
    observation = profile['native_values']['carbs_g'][0]
    if issue == 'missing': profile['native_values'].pop('carbs_g')
    elif issue == 'malformed': profile['native_values']['carbs_g'].append(None)
    elif issue == 'conflict': profile['native_values']['carbs_g'].append({**observation, 'value': 1.0})
    elif issue == 'unit': observation['raw_unit'] = 'Gram/100 millilitre'
    elif issue == 'value': observation['value'] = 1.0
    elif issue == 'raw_value': observation['raw_value'] = '1'
    elif issue == 'row': observation['source_csv_row'] = True
    elif issue == 'file': profile['file'] = 'unverified.csv'
    else:
        for item in (row['annotation'], row['source_record'], profile):
            item['source']['release'] = 'unknown'
    assert 'carbs_g' not in adapter._composition(row).nutrient_definitions


def test_quoted_anatomy_recovery_reaches_snapshot_without_changing_saved_data(database):
    active, source, *_ = database
    text = source['annotation']['text_annotation']
    text['main_ingredient'] = 'chicken'
    start = text['parsed_text'].index('chicken')
    text['foods'] = [{'food_id':'food:0','identity':'chicken','family':'generic',
                     'start':start,'end':start+7,'polarity':'asserted'}]
    for field in ('food_part','biological_source'):
        text['facets'][field] = None
    text['evidence'] = [item for item in text['evidence']
                        if item['field'] not in {'facets.food_part','facets.biological_source'}]
    active['profile']['details'][0]['selected_composition_facts'] = deepcopy(source['annotation'])
    before_active, before_source = deepcopy(active), deepcopy(source)
    composition = adapter.load_active_snapshot('recipe-1','irish').ingredients[0].composition
    assert composition.properties['food_part'].value == 'thigh'
    assert composition.properties['food_part'].evidence.status == 'stated'
    assert composition.properties['food_part'].evidence.method == 'scoped_anatomy_v2'
    assert composition.properties['biological_source'].value == 'chicken'
    assert {item['field'] for item in composition.provenance['label_evidence_recovery']} == {
        'food_part','biological_source'}
    assert composition.values_per_100g == source['annotation']['values_per_100g']
    assert active == before_active and source == before_source


def test_preview_with_missing_basis_does_not_manufacture_a_nutritional_gain(database):
    active, source, target, edit, reads, lookup = database
    result = adapter.preview_raw_part("irish", edit)
    assert result.status == "needs_evidence"
    assert "occurrence.raw_edible_mass" in result.missing_evidence
    assert result.before is result.after is None
    assert result.source_composition.ref.candidate_id == "candidate-thigh"
    assert result.target_composition.ref.candidate_id == "candidate-breast"
    assert result.culinary_status == "not_evaluated" and result.instruction_status == "pending"
    assert reads.call_count == 2
    assert lookup.call_count == 2
    result.model_dump_json()


def test_production_snapshot_retains_exact_reviewed_fills_without_inventing_definitions(database, monkeypatch):
    active, source, *_ = database
    detail = active["profile"]["details"][0]
    fill = {"source": "reviewed-same-food", "value": 0.8}
    monkeypatch.setitem(adapter.RECORD_FILLS, "native-thigh", {"fibre_g": fill})
    detail["values_per_100g"]["fibre_g"] = fill["value"]
    detail["nutrients_filled"] = {"fibre_g": fill["source"]}
    before = deepcopy(active), deepcopy(source)
    snapshot = adapter.load_active_snapshot("recipe-1", "irish", allow_input_estimates=True)
    composition = snapshot.ingredients[0].composition
    assert composition.values_per_100g["fibre_g"] == 0.8
    assert "fibre_g" not in composition.nutrient_definitions
    assert composition.provenance["reviewed_nutrient_fills"] == {"fibre_g": fill}
    composition.provenance["reviewed_nutrient_fills"]["fibre_g"]["value"] = 99
    assert fill["value"] == 0.8
    assert (active, source) == before


def test_saved_source_context_empty_tuple_and_list_are_the_same_json_value(database):
    active, source, *_ = database
    source['annotation']['text_annotation']['context'] = {'neighbouring_ingredients': ()}
    active['profile']['details'][0]['selected_composition_facts']['text_annotation']['context'] = {
        'neighbouring_ingredients': []}
    snapshot = adapter.load_active_snapshot('recipe-1', 'irish', allow_input_estimates=True)
    assert snapshot.ingredients[0].composition.ref.candidate_id == 'candidate-thigh'


@pytest.mark.parametrize("issue", ["unreviewed", "wrong_source", "wrong_value", "known_value", "verified_path"])
def test_reviewed_fill_support_cannot_authorize_other_saved_changes(database, monkeypatch, issue):
    active, source, *_ = database
    detail = active["profile"]["details"][0]
    fill = {"source": "reviewed-same-food", "value": 0.8}
    metric = "energy_kcal" if issue == "known_value" else "fibre_g"
    monkeypatch.setitem(adapter.RECORD_FILLS, "native-thigh", {metric: fill} if issue != "unreviewed" else {})
    detail["values_per_100g"][metric] = 0.9 if issue == "wrong_value" else 0.8
    detail["nutrients_filled"] = {metric: "other-source" if issue == "wrong_source" else fill["source"]}
    with pytest.raises(adapter.ComparisonLoadError, match="saved_selection_source_mismatch") as error:
        adapter.load_active_snapshot("recipe-1", "irish", allow_input_estimates=issue != "verified_path")
    assert error.value.blocked


@pytest.mark.parametrize("change,reason", [("run", "baseline_mismatch"), ("quantities", "baseline_mismatch"),
                                          ("release", "target_release_mismatch"), ("region", "target_region_mismatch")])
def test_stale_or_out_of_scope_requests_never_lookup_a_target(database, change, reason):
    active, source, target, edit, reads, lookup = database
    if change == "run": edit.baseline_run_id = "old-run"
    elif change == "quantities": edit.quantity_input_hash = "old-quantities"
    elif change == "release": edit.to_composition.release_id = "another-release"
    else: edit.to_composition.collection = "nutritional_ingredients_hungarian"
    result = adapter.preview_raw_part("irish", edit)
    assert result.status == "blocked" and reason in result.blockers
    assert lookup.call_count == 1


@pytest.mark.parametrize("removed", [False, True])
def test_active_pointer_is_rechecked_before_returning(database, removed):
    active, source, target, edit, reads, lookup = database
    reads.side_effect = [active, None if removed else {**active, "run_id": "new-run"}]
    result = adapter.preview_raw_part("irish", edit)
    assert result.status == "blocked" and result.blockers == ["active_profile_changed"]
    assert result.before is result.after is None


def test_missing_active_profile_does_not_use_latest_staged_run(database):
    active, source, target, edit, reads, lookup = database
    reads.return_value = None
    assert adapter.preview_raw_part("irish", edit).missing_evidence == ["active_profile_not_found"]
    lookup.assert_not_called()


def test_missing_target_is_unavailable_without_name_or_native_fallback(database):
    active, source, target, edit, reads, lookup = database
    edit.to_composition.candidate_id = "absent"
    assert adapter.preview_raw_part("irish", edit).missing_evidence == ["target_composition_not_found"]


def test_predecessor_with_no_retrieval_id_can_use_one_exact_native_record(database):
    active, source, target, edit, reads, lookup = database
    del active["profile"]["details"][0]["selected_candidate_id"]
    snapshot = adapter.load_active_snapshot("recipe-1", "irish")
    lookup.assert_called_once_with("release-1", "nutritional_ingredients_eu", native_id="native-thigh")
    assert snapshot.ingredients[0].composition.ref.candidate_id == "candidate-thigh"
    assert snapshot.ingredients[0].composition.provenance["selection_resolution"] == "unique_exact_native_id"


def test_ambiguous_native_record_does_not_pick_an_arbitrary_retrieval_id(database):
    active, source, target, edit, reads, lookup = database
    del active["profile"]["details"][0]["selected_candidate_id"]
    lookup.side_effect = None
    lookup.return_value = [source, {**source, "candidate_id": "duplicate"}]
    result = adapter.preview_raw_part("irish", edit)
    assert result.status == "needs_evidence" and result.missing_evidence == ["ambiguous_selected_native_id"]


@pytest.mark.parametrize("change,reason", [
    ("owner", "active_profile_ownership_mismatch"), ("region", "profile_region_mismatch"),
    ("release", "profile_release_mismatch"), ("quantity_hash", "occurrence_quantity_hash_mismatch"),
    ("occurrence_release", "occurrence_release_mismatch"), ("saved_value", "saved_selection_source_mismatch"),
    ("native_value", "released_nutrient_values_mismatch"), ("native_id", "native_nutrient_identity_mismatch"),
    ("profile_value", "native_profile_values_mismatch"), ("source_identity", "released_source_identity_mismatch"),
    ("unit", "released_nutrient_values_mismatch"), ("basis", "unsupported_source_reporting_basis"),
    ("canonical_id", "saved_selection_identity_mismatch"),
    ("quantity_evidence", "saved_quantity_evidence_mismatch"),
])
def test_inconsistent_records_cannot_enter_comparison(database, change, reason):
    active, source, target, edit, reads, lookup = database
    detail = active["profile"]["details"][0]
    if change == "owner": active["recipe_id"] = "other"
    elif change == "region": active["profile"]["source_key"] = "hungarian"
    elif change == "release": active["profile"]["catalogue_release"] = "other"
    elif change == "quantity_hash": detail["quantity_input_hash"] = "other"
    elif change == "occurrence_release": detail["catalogue_release"] = "other"
    elif change == "saved_value": detail["values_per_100g"]["energy_kcal"] = 999
    elif change == "native_value": target["nutrient_row"]["nutrients"]["Energy, kcal"]["value"] = 999
    elif change == "native_id": target["nutrient_row"]["id"] = "other"
    elif change == "profile_value": target["source_profile"] = deepcopy(target["source_profile"]); target["source_profile"]["values_per_100g"]["energy_kcal"] = 999
    elif change == "source_identity": target["source_profile"] = deepcopy(target["source_profile"]); target["source_profile"]["source"]["release"] = "other"
    elif change == "unit": target["nutrient_row"]["nutrients"]["Energy, kcal"]["unit"] = "g"
    elif change == "canonical_id": detail["canonical_food_id"] = "other"
    elif change == "quantity_evidence": detail["quantity_evidence"]["weight_g"] = 300
    else: target["annotation"]["basis"]["unit"] = "ml"
    result = adapter.preview_raw_part("irish", edit)
    assert result.status == "blocked" and result.blockers == [reason]
    assert result.after is None


def test_legacy_basis_stays_a_proxy_and_is_not_upgraded(database):
    active, source, target, edit, reads, lookup = database
    target["annotation"]["basis"]["evidence"] = "legacy_contract_assumption"
    result = adapter.preview_raw_part("irish", edit)
    assert result.target_composition.basis.evidence.status == "proxy"
    assert "target.reporting_basis" in result.missing_evidence


def test_native_conflicts_cannot_resurrect_the_original_text_assertion(database):
    active, source, target, edit, reads, lookup = database
    target["annotation"]["native_annotation"] = {"facets": {"preparation_state": None}}
    result = adapter.preview_raw_part("irish", edit)
    assert result.target_composition.properties["preparation_state"].value is None
    assert result.target_composition.properties["preparation_state"].evidence.status == "inferred"


def test_db_outages_are_not_reported_as_missing_food_evidence(database):
    active, source, target, edit, reads, lookup = database
    reads.side_effect = OperationalError("SELECT", {}, Exception("offline"))
    with pytest.raises(OperationalError):
        adapter.preview_raw_part("irish", edit)


def test_malformed_values_are_blocked_without_publishing_totals(database):
    active, source, target, edit, reads, lookup = database
    active["profile"]["details"][0]["weight_g"] = float("nan")
    result = adapter.preview_raw_part("irish", edit)
    assert result.status == "blocked" and result.blockers == ["saved_quantity_evidence_mismatch"]


def test_recovered_and_inferred_properties_retain_their_distinct_evidence(database):
    active, source, target, edit, reads, lookup = database
    observed = active["profile"]["details"][0]["observed_facts"]
    observed["evidence"] = []
    observed["occurrence_recovery"] = {
        "original_text": "raw chicken thigh from a can", "conflicts": [], "recovered": [
            {"field": "preparation_state", "value": "raw", "evidence": [{"quote": "raw", "status": "stated"}]},
            {"field": "preservation_methods", "value": ["canned"],
             "evidence": [{"quote": "can", "status": "inferred_from_container"}]},
        ],
    }
    observed["facets"]["preservation_methods"] = ["canned"]
    snapshot = adapter.load_active_snapshot("recipe-1", "irish")
    properties = snapshot.ingredients[0].observed_properties
    assert properties["preparation_state"].evidence.status == "stated"
    assert properties["preservation_methods"].value == ["canned"]
    assert properties["preservation_methods"].evidence.status == "inferred"


def test_missing_unrelated_selection_is_retained_as_an_unresolved_occurrence(database):
    active, source, target, edit, reads, lookup = database
    other = deepcopy(active["profile"]["details"][0])
    other.update(position=4, selected_candidate_id="absent", ingredient="unresolved ingredient")
    active["profile"]["details"].append(other)
    snapshot = adapter.load_active_snapshot("recipe-1", "irish")
    assert snapshot.ingredients[1].position == 4
    assert snapshot.ingredients[1].composition is None
    assert snapshot.ingredients[1].quantity.weight_g == 200
    assert snapshot.provenance["loading_issues"] == ["occurrence:4.selected_composition_not_found"]


def test_missing_selected_retrieval_id_is_not_replaced_using_native_id(database):
    active, source, target, edit, reads, lookup = database
    active["profile"]["details"][0]["selected_candidate_id"] = "absent"
    snapshot = adapter.load_active_snapshot("recipe-1", "irish")
    assert snapshot.ingredients[0].composition is None
    lookup.assert_called_once_with("release-1", "nutritional_ingredients_eu", candidate_id="absent")


@pytest.mark.parametrize("kwargs", [{}, {"candidate_id": "x", "native_id": "x"}])
def test_exact_reader_requires_one_kind_of_key(kwargs):
    with pytest.raises(ValueError, match="exactly one"):
        matching_store.composition_records("r", "collection", **kwargs)


@pytest.mark.parametrize("field", ["candidate_id", "native_id"])
def test_composition_reader_is_parameterized_and_only_selects_ready_records(monkeypatch, field):
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.mappings.return_value = [{"candidate_id": "x"}]
    monkeypatch.setattr(matching_store, "get_engine", lambda: engine)
    identifier = "quoted ' identifier"
    assert matching_store.composition_records("r", "collection", **{field: identifier}) == [{"candidate_id": "x"}]
    sql, params = connection.execute.call_args.args
    assert str(sql).lstrip().startswith("SELECT")
    assert "r.status='ready'" in str(sql) and f"c.{field}=:identifier" in str(sql)
    assert identifier not in str(sql) and params["identifier"] == identifier
    connection.execute.assert_called_once()


@pytest.mark.parametrize("include_provenance", [False, True])
def test_active_reader_keeps_existing_api_shape_unless_provenance_is_requested(monkeypatch, include_provenance):
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.mappings.return_value.first.return_value = {"run_id": "run-1"}
    monkeypatch.setattr(matching_store, "get_engine", lambda: engine)
    assert matching_store.active_profile("recipe-1", "irish", include_provenance=include_provenance) == {"run_id": "run-1"}
    sql, params = connection.execute.call_args.args
    columns = str(sql).split("FROM")[0]
    assert ("r.input_hash" in columns) is include_provenance
    assert ("r.recipe_id" in columns) is include_provenance
    assert params == {"id": "recipe-1", "source": "irish"}


@pytest.mark.parametrize("changed_run", [False, True])
def test_declaration_is_a_separate_comparison_layer_and_does_not_approve_source_gaps(database, changed_run):
    active, source, target, edit, reads, lookup = database
    before = deepcopy(active)
    declaration = QuantityBasisDeclaration(
        recipe_id=edit.recipe_id, baseline_run_id=edit.baseline_run_id,
        quantity_input_hash=edit.quantity_input_hash, position=0, weight_g=200, basis="raw_edible",
        declared_by="synthetic-user", statement="I confirm this is 200 g of raw edible chicken.",
        evidence={"reference": "synthetic-confirmation/1", "method": "user_confirmation", "version": "1"},
    )
    if changed_run:
        reads.side_effect = [active, {**active, "run_id": "changed-run"}]
    result = adapter.preview_raw_part("irish", edit, quantity_declarations=[declaration])
    assert active == before
    if changed_run:
        assert result.blockers == ["active_profile_changed"]
    else:
        assert result.status == "needs_evidence"
        assert result.baseline_quantity.basis == "unknown"
        assert result.quantity.basis == "raw_edible"
        assert result.quantity_declarations[0].evidence.method == "user_confirmation"
        assert "occurrence.raw_edible_mass" not in result.missing_evidence
        assert "source.property:processes" in result.missing_evidence
        assert result.after is None


def test_new_source_mass_evidence_blocks_stale_baseline_without_mutation(database):
    active = database[0]
    detail = active['profile']['details'][0]
    detail.update(ingredient='reduced-fat ricotta cheese', original_ingredient_text='⅔ cup (150g) reduced-fat ricotta cheese',
                  weight_g=88.44, quantity_evidence=None, quantity_status='retained_input_quantity_unverified')
    before = deepcopy(active)
    with pytest.raises(adapter.ComparisonLoadError, match='source_mass_requires_baseline_recalculation'):
        adapter.load_active_snapshot('recipe-1', 'irish', allow_input_estimates=True)
    assert active == before


def test_label_state_reads_the_state_a_record_label_states():
    from recipe_wrangler.services.adaptation.comparison_store import label_state
    assert label_state('Beef, stewing steak, raw, lean') == 'raw'
    assert label_state('Beef, topside, microwaved, lean') == 'cooked'
    assert label_state('Chicken, wing quarter, casseroled, meat only') == 'cooked'
    assert label_state('Apples, cooking, baked with sugar, flesh only') == 'cooked'
    assert label_state('lettuce') is None and label_state('Salmon raw, smoked cooked') is None
