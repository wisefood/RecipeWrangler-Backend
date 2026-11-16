"""Safety properties for the offline source-aware sustainability pilot."""

import sqlite3

import pytest

from scripts.sustainability import reference_catalogue as catalogue
from scripts.sustainability.reference_catalogue import add_legacy_reference, add_reference, food_key, initialize, lookup


def reference(source="AGRIBALYSE", record_id="11000", name="Garlic, fresh", value=0.383, market="FR"):
    return {"source": source, "version": "fixture-v1", "source_record_id": record_id,
            "source_url": "https://example.org/source", "source_sha256": "fixture-hash",
            "name": name, "market": market, "basis": "kg_source_product",
            "metrics": {"climate_change": {"value": value, "unit": "kg CO2e/kg"}}}


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    initialize(connection)
    yield connection
    connection.close()


def test_repeated_import_is_idempotent_and_keeps_source(db):
    assert add_reference(db, reference())
    assert not add_reference(db, reference())
    result = lookup(db, "garlic")
    assert len(result["references"]) == 1
    assert result["references"][0]["source_record_id"] == "11000"
    assert result["references"][0]["source_url"] == "https://example.org/source"


def test_same_food_different_sources_are_not_dropped_or_averaged(db):
    add_reference(db, reference())
    add_reference(db, reference("CONCITO", "Ra00136-FR", "Garlic, raw", 1.181))
    result = lookup(db, "garlic")
    assert result["status"] == "multiple_references"
    assert result["selection"] is None
    assert {r["source"] for r in result["references"]} == {"AGRIBALYSE", "CONCITO"}
    assert len({r["food_key"] for r in result["references"]}) == 1


@pytest.mark.parametrize("name", ["garlic powder", "Garlic, powder, dried", "garlic salt", "garlic mayonnaise", "garlic in oil", "garlic baguette"])
def test_raw_garlic_lookup_never_includes_other_forms_or_compounds(db, name):
    add_reference(db, reference())
    add_reference(db, reference(record_id="different", name=name))
    assert [r["source_record_id"] for r in lookup(db, "garlic")["references"]] == ["11000"]
    assert food_key(name) != "garlic"


def test_conflicting_value_for_same_source_record_requires_review(db):
    add_reference(db, reference())
    with pytest.raises(ValueError, match="Conflicting values"):
        add_reference(db, reference(value=5.0))
    assert lookup(db, "garlic")["references"][0]["metrics"]["climate_change"]["value"] == 0.383


def test_market_filter_does_not_fabricate_a_missing_market(db):
    add_reference(db, reference("CONCITO", "Ra00136-FR"))
    assert lookup(db, "garlic", market="IE")["status"] == "not_found"


def test_conflicting_legacy_key_is_quarantined_instead_of_first_wins(db):
    record = reference("legacy", value=0.67)
    assert add_legacy_reference(db, record) == "inserted"
    assert add_legacy_reference(db, {**record, "category": "different"}) == "quarantined_conflict"
    assert lookup(db, "garlic")["status"] == "not_found"
    assert add_legacy_reference(db, record) == "quarantined_skipped"
    assert db.execute("SELECT count(*) FROM quarantined").fetchone()[0] == 1


def test_source_filter_and_kg_or_litre_warning_are_preserved(db):
    record = reference("SU-EATABLE LIFE", "garlic")
    record["basis"] = "kg_or_litre_unresolved"
    record["metrics"]["climate_change"]["unit"] = "kg CO2e/(kg OR litre)"
    add_reference(db, record)
    add_reference(db, reference())
    result = lookup(db, "garlic", source="SU-EATABLE LIFE")
    assert len(result["references"]) == 1
    assert result["references"][0]["basis"] == "kg_or_litre_unresolved"


def test_agribalyse_variant_suffix_is_not_dropped(monkeypatch, tmp_path):
    monkeypatch.setattr(catalogue, "source_info", lambda *args: {"source": "AGRIBALYSE", "version": "test"})
    monkeypatch.setattr(catalogue, "xlsx_rows", lambda *args: iter([
        (2, {"N": "Climate change"}), (3, {"N": "kg CO2e/kg"}),
        (4, {"A": "13167_1", "F": "Cereal powder", "N": "2.2", "L": "2"}),
    ]))
    kind, record = next(catalogue.public_records(tmp_path))
    assert kind == "record"
    assert record["source_record_id"] == "13167_1"


def test_water_only_reference_cannot_answer_carbon_lookup(db):
    record = reference("SU-EATABLE LIFE", "garlic")
    record["metrics"] = {"water_footprint": {"value": 100, "unit": "litre/kg"}}
    add_reference(db, record)
    assert lookup(db, "garlic")["status"] == "not_found"
    assert lookup(db, "garlic", metric="water_footprint")["status"] == "matched"


def test_su_eatable_case_sensitive_variant_codes_are_not_overwritten(monkeypatch, tmp_path):
    monkeypatch.setattr(catalogue, "source_info", lambda *args: {"source": "SU-EATABLE LIFE", "version": "test"})
    monkeypatch.setattr(catalogue, "xlsx_rows", lambda path, sheet: iter([
        (2, {"B": "CUCUMBER (G)", "C": "2.54"}),
        (3, {"B": "CUCUMBER (g)", "C": "0.32"}),
    ] if sheet == "SEL CF for users" else []))
    records = [record for _, record in catalogue.su_eatable_records(tmp_path / "source.xlsx")]
    assert {r["source_record_id"] for r in records} == {"CUCUMBER (G)", "CUCUMBER (g)"}
    assert {r["metrics"]["climate_change"]["value"] for r in records} == {2.54, 0.32}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True])
def test_invalid_factor_cannot_enter_catalogue(db, value):
    with pytest.raises(ValueError, match="invalid environmental"):
        add_reference(db, reference(value=value))
