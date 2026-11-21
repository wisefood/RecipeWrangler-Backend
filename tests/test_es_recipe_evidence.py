from recipe_wrangler.utils.es_recipe_evidence import (
    normalize_allergen_evidence,
    normalize_consumer_suitability,
    suitable_groups,
)


def test_normalizes_allergen_evidence_without_losing_pairing() -> None:
    evidence = normalize_allergen_evidence(
        [
            {
                "allergen": "Milk",
                "ingredient": "Whey Powder",
                "ingredient_id": "ingredient-1",
                "declaration_id": "declaration-1",
                "presence": "contains",
                "evidence_status": "inferred",
                "sources": ["keyword"],
                "foodon_ids": [],
                "keyword_matches": ["Whey"],
                "classification_version": "fato-foodon-v1",
            }
        ]
    )

    assert evidence == [
        {
            "allergen": "milk",
            "ingredient": "whey powder",
            "ingredient_id": "ingredient-1",
            "declaration_id": "declaration-1",
            "presence": "contains",
            "evidence_status": "inferred",
            "sources": ["keyword"],
            "foodon_ids": [],
            "keyword_matches": ["whey"],
            "classification_version": "fato-foodon-v1",
        }
    ]


def test_consumer_suitability_always_contains_all_supported_groups() -> None:
    assessments = normalize_consumer_suitability(
        [
            {
                "group": "Vegetarian",
                "status": "suitable",
                "blocking_ingredients": [],
                "reason_codes": ["all_ingredients_suitable"],
                "sources": ["ingredient_suitability"],
                "classification_version": "vegan-vegetarian-v1",
            }
        ],
        classification_version="vegan-vegetarian-v1",
    )

    assert [item["group"] for item in assessments] == [
        "vegan",
        "vegetarian",
        "pescatarian",
    ]
    assert assessments[0]["status"] == "unknown"
    assert assessments[1]["status"] == "suitable"
    assert suitable_groups(assessments) == ["vegetarian"]



def test_conflicting_statuses_cannot_hide_positive_evidence_by_input_order():
    from recipe_wrangler.utils.es_recipe_evidence import recipe_allergen_fields
    absent = {"allergen": "milk", "ingredient": "sauce", "presence": "absent"}
    positive = {**absent, "presence": "contains"}
    possible = {**absent, "presence": "possible"}
    expected = normalize_allergen_evidence([absent, positive, possible])
    assert expected == normalize_allergen_evidence([possible, positive, absent])
    assert {item["presence"] for item in expected} == {"absent", "contains", "possible"}
    assessment = recipe_allergen_fields(None, expected)["allergen_assessment"]
    assert "milk" in assessment["detected_allergens"]
    assert "milk" not in assessment["possible_allergens"]
    assert "egg" in assessment["unresolved_allergens"]


def test_status_preservation_still_deduplicates_exact_evidence():
    row = {"allergen": "milk", "ingredient": "sauce", "presence": "possible"}
    assert len(normalize_allergen_evidence([row, row])) == 1


def test_current_claim_overrides_stale_suitable_status_without_changing_input():
    from copy import deepcopy
    original = [{'group':'vegan', 'status':'suitable', 'reason_codes':['all_ingredients_suitable'], 'classification_version':'v'},
                {'group':'vegetarian', 'status':'suitable', 'classification_version':'v'}]
    before = deepcopy(original)
    assessments = normalize_consumer_suitability(original, classification_version='v', allergens=['dairy'])
    assert [a['status'] for a in assessments] == ['not_suitable', 'suitable', 'unknown']
    assert 'allergen_evidence' in assessments[0]['reason_codes']
    assert 'all_ingredients_suitable' not in assessments[0]['reason_codes']
    assert original == before


def test_possible_conflict_downgrades_suitability_without_inventing_known_presence():
    evidence = [{'allergen':'fish', 'presence':'may_contain'}]
    assessments = normalize_consumer_suitability(
        [{'group':'vegan', 'status':'not_suitable'}, {'group':'vegetarian', 'status':'suitable', 'classification_version':'v'}],
        classification_version='v', evidence=evidence,
    )
    assert [a['status'] for a in assessments] == ['not_suitable', 'unknown', 'unknown']
    assert 'possible_allergen_presence' in assessments[1]['reason_codes']


def test_explicit_unknown_conflict_cannot_keep_stale_suitable_status():
    assessments = normalize_consumer_suitability(
        [{'group':'vegan', 'status':'suitable', 'classification_version':'v'}], classification_version='v',
        evidence=[{'allergen':'egg', 'presence':'unknown'}],
    )
    assert assessments[0]['status'] == 'unknown'
    assert 'unresolved_allergen_evidence' in assessments[0]['reason_codes']


def test_free_from_conflicts_use_aliases_and_never_add_absence_tags():
    from recipe_wrangler.utils.es_recipe_evidence import assessed_diet_tags
    assert assessed_diet_tags(['nut_free', 'gluten_free', 'dairy_free'], [], allergens=['nuts', 'dairy']) == ['gluten_free']
    assert assessed_diet_tags([], [], allergens=[]) == []
    assert assessed_diet_tags(['dairy_free'], [], evidence=[{'allergen':'milk', 'presence':'possible'}]) == []


def test_old_positive_policy_version_cannot_be_presented_as_current_assessment():
    rows=normalize_consumer_suitability([
        {'group':'vegan','status':'suitable','classification_version':'vegan-vegetarian-v1'},
        {'group':'vegetarian','status':'not_suitable','classification_version':'vegan-vegetarian-v1'},
    ],classification_version='vegan-vegetarian-v2')
    assert rows[0]['status']=='unknown'
    assert rows[0]['reason_codes']==['unsupported_classification_version']
    assert rows[1]['status']=='not_suitable'


def test_missing_policy_version_cannot_be_promoted_to_current_eligibility():
    from recipe_wrangler.utils.es_recipe_evidence import recipe_consumer_fields
    fields=recipe_consumer_fields([{'group':group,'status':'suitable'} for group in ['vegan','vegetarian','pescatarian']])
    assert fields['suitable_for']==[]
    assert all(item['status']=='unknown' for item in fields['consumer_suitability'])
