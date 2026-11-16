from scripts.neo4j.preview_allergen_reconciliation import (
    ancestry, canonical, digest, preview_ingredient,
)


def fixture():
    classes = {
        "egg": {"properties":{"foodon_id":"FOODON_00001275"}, "parents":[]},
        "wheat": {"properties":{"foodon_id":"FOODON_00001164"}, "parents":[]},
    }
    ingredient = {"element_id":"i", "properties":{"canonical_id":"i", "name":"chicken egg"},
                  "links":[{"target":"egg", "foodon_id":"FOODON_00001275"}],
                  "edges":[], "declarations":[]}
    return ingredient, classes


def derived(allergen, **overrides):
    props = {"presence":"contains", "evidence_status":"inferred", "sources":["foodon"],
             "classification_version":"fato-foodon-v1", **overrides}
    return {"allergen":allergen, "properties":props}


def group(result, name):
    return next(g for g in result["groups"] if g["allergen"] == name)


def test_old_invalid_root_is_candidate_not_absence():
    ingredient, classes = fixture()
    ingredient["edges"] = [derived("gluten")]
    result = preview_ingredient(ingredient, classes, {}, lambda *_: [])
    assert group(result, "gluten")["action"] == "retire_derived_candidate"
    assert group(result, "gluten")["assessment"] == "unresolved"
    assert result["before"] == ingredient


def test_previous_v4_rule_evidence_remains_owned_after_v5_upgrade():
    ingredient, classes = fixture()
    ingredient['edges'] = [derived('gluten', classification_version='fato-foodon-v4')]
    result = preview_ingredient(ingredient, classes, {}, lambda *_: [])
    assert group(result, 'gluten')['action'] == 'retire_derived_candidate'


def test_complete_other_ancestry_preserves_valid_support_without_keyword():
    from recipe_wrangler.utils.food_ontology import ALLERGEN_DETECTION_RULES
    ingredient, classes = fixture()
    classes["wheat"]["properties"]["foodon_id"] = ALLERGEN_DETECTION_RULES["gluten"]["roots"][0]
    classes["egg"]["parents"] = [{"target":"wheat"}]
    ingredient["edges"] = [derived("gluten")]
    result = preview_ingredient(ingredient, classes, {}, lambda *_: [])
    assert group(result, "gluten")["action"] == "retain_supported"
    assert group(result, "gluten")["proposed_root_support"][0]["path"] == ["egg", "wheat"]


def test_keyword_and_foreign_provenance_survive():
    ingredient, classes = fixture()
    ingredient["edges"] = [derived("gluten"), derived("fish", sources=["manual"])]
    result = preview_ingredient(ingredient, classes, {}, lambda _, a: ["bread"] if a == "gluten" else [])
    assert group(result, "gluten")["action"] == "retain_supported"
    assert group(result, "fish")["action"] == "retain_unresolved"


def test_alternative_formulation_cannot_retire_stored_positive():
    ingredient, classes = fixture()
    ingredient["properties"]["name"] = "low-fat milk or plant-based alternative"
    ingredient["edges"] = [derived("milk", sources=["keyword"])]
    result = preview_ingredient(ingredient, classes, {}, lambda *_: [])
    assert "alternative_formulation_requires_source_review" in result["blockers"]
    assert group(result, "milk")["action"] == "retain_unresolved"


def test_new_ontology_positive_requires_supported_link_identity():
    from recipe_wrangler.utils.food_ontology import ALLERGEN_DETECTION_RULES
    ingredient, classes = fixture()
    ingredient["properties"]["name"] = "millet groats"
    ingredient["links"] = [{"target":"wheat"}]
    classes["wheat"]["properties"] = {
        "foodon_id":ALLERGEN_DETECTION_RULES["gluten"]["roots"][0], "label":"barley groats"}
    result = preview_ingredient(ingredient, classes, {}, lambda *_: [])
    gluten = group(result, "gluten")
    assert gluten["action"] == "hold_new_positive_identity"
    assert gluten["assessment"] == "unresolved"
    assert gluten["proposed_evidence"] is None
    assert gluten["proposed_root_support"]
    ingredient["properties"]["name"] = "barley groats"
    result = preview_ingredient(ingredient, classes, {}, lambda *_: [])
    assert group(result, "gluten")["action"] == "add_supported"


def test_unresolved_link_proposal_blocks_retirement():
    ingredient, classes = fixture()
    ingredient["edges"] = [derived("fish")]
    proposals = {"i":{"name":"chicken egg", "stored_class_id":"FOODON_00001275", "proposed_class_id":""}}
    result = preview_ingredient(ingredient, classes, proposals, lambda *_: [])
    assert result["blockers"] == ["replacement_identity_unresolved"]
    assert group(result, "fish")["action"] == "retain_unresolved"


def test_multiple_links_and_proposal_preserve_unrelated_link():
    ingredient, classes = fixture()
    classes["new"] = {"properties":{"foodon_id":"new"}, "parents":[]}
    ingredient["links"].append({"target":"wheat", "foodon_id":"FOODON_00001164"})
    proposals = {"i":{"name":"chicken egg", "stored_class_id":"FOODON_00001275", "proposed_class_id":"new"}}
    result = preview_ingredient(ingredient, classes, proposals, lambda *_: [])
    assert {l["target"] for l in result["proposed_links"]} == {"wheat", "new"}


def test_ancestry_handles_cycles_and_rejects_incomplete_capture():
    import pytest
    _, classes = fixture()
    classes["egg"]["parents"] = [{"target":"wheat"}]
    classes["wheat"]["parents"] = [{"target":"egg"}]
    assert set(ancestry("egg", classes)) == {"egg", "wheat"}
    classes["wheat"]["parents"] = [{"target":"missing"}]
    with pytest.raises(ValueError, match="Incomplete"):
        ancestry("egg", classes)


def test_fingerprints_ignore_relationship_list_order():
    assert digest(canonical({"edges":[{"id":1}, {"id":2}]})) == digest(canonical({"edges":[{"id":2}, {"id":1}]}))


def test_capture_preserves_property_array_order_for_rollback():
    value = {"properties":{"source_labels":["z", "a"]}, "edges":[]}
    assert canonical(value) == value


def test_unknown_declaration_provenance_is_never_retired():
    ingredient, classes = fixture()
    ingredient["declarations"] = [{"properties":{"presence":"contains"},
                                     "concerns":[{"allergen":"gluten"}]}]
    result = preview_ingredient(ingredient, classes, {}, lambda *_: [])
    assert group(result, "gluten")["action"] == "retain_unresolved"


def test_proposed_evidence_preserves_keyword_when_bad_link_withdrawn():
    ingredient, classes = fixture()
    ingredient["edges"] = [derived("gluten")]
    proposals = {"i":{"name":"Chicken egg", "stored_class_id":"FOODON_00001275", "proposed_class_id":""}}
    result = preview_ingredient(ingredient, classes, proposals, lambda _, a: ["bread"] if a == "gluten" else [])
    assert result["proposed_links"] == []
    assert group(result, "gluten")["proposed_evidence"]["sources"] == ["keyword"]
    assert result["blockers"] == ["replacement_identity_unresolved"]


def test_full_preview_retains_recipe_claims_and_rejects_tampering(tmp_path):
    import csv
    import gzip
    import hashlib
    import json
    import pytest
    from scripts.neo4j.preview_allergen_reconciliation import preview, QUERIES, encoded
    from recipe_wrangler.utils.food_ontology import ALLERGEN_DETECTION_RULES

    roots = sorted({r for rule in ALLERGEN_DETECTION_RULES.values() for r in rule["roots"]})
    ingredient, _ = fixture()
    ingredient["links"] = []
    rows = {"ingredients":[ingredient],
            "classes":[{"element_id":root, "properties":{"foodon_id":root}, "parents":[]} for root in roots],
            "recipes":[{"element_id":"r", "properties":{"recipe_id":"r", "declared_allergens":["peanut"]},
                        "ingredients":[{"target":"i"}], "suitability":[], "tags":[]}]}
    ingredient["edges"] = [derived("gluten")]
    ingredient["declarations"] = [{"properties":{"presence":"may_contain", "sources":["manual"]},
                                     "concerns":[{"allergen":"soy"}]}]
    hashes = {}
    for kind in QUERIES:
        content = "".join(encoded(r)+"\n" for r in rows[kind]).encode()
        with gzip.open(tmp_path / f"{kind}.jsonl.gz", "wb") as stream:
            stream.write(content)
        hashes[f"{kind}:0"] = {"sha256":hashlib.sha256(content).hexdigest()}
    (tmp_path / "capture.json").write_text(json.dumps({"stable":True, "smoke_only":False, "hashes":hashes}))
    proposals = tmp_path / "proposals.csv"
    with proposals.open("w") as stream:
        csv.writer(stream).writerow(["ingredient_id"])
    preview(tmp_path, proposals)
    with gzip.open(tmp_path / "affected_recipes.jsonl.gz", "rt") as stream:
        affected = json.loads(next(stream))
    assert affected["retained_recipe_claims"] == ["peanut"]
    assert "peanut" not in affected["unresolved_groups"]
    assert affected["allergen_uncertainty"] is True
    assert affected["conditional_card"]["allergens"] == ["egg", "peanut"]
    assert affected["conditional_card"]["allergen_assessment"]["cross_contact_assessed"] is False
    assert affected["conditional_card"]["allergen_assessment"]["possible_allergens"] == ["soy"]
    assert "gluten" in affected["conditional_card"]["allergen_assessment"]["unresolved_allergens"]
    with gzip.open(tmp_path / "ingredients.jsonl.gz", "wt") as stream:
        stream.write("{}\n")
    with pytest.raises(ValueError, match="fingerprint"):
        preview(tmp_path, proposals)
