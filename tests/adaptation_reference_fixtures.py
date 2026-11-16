"""Source-pinned environmental records for offline adaptation tests."""


def emissions_reference(name, value):
    return {"name": name, "source": "AGRIBALYSE", "version": "3.2-food-AOUT25",
        "method": "AGRIBALYSE_EF3.1", "market": "FR", "basis": "kg_consumed_product",
        "boundary": "consumer_product_including_intermediate_losses_and_declared_preparation",
        "scenario": {"preparation": "Pas de préparation"}, "reference_id": "fixture:" + name,
        "source_record_id": name, "source_url": "https://example.org/environmental-source",
        "source_sha256": "fixture-source-hash",
        "metrics": {"climate_change": {"value": value, "unit": "kg CO2 eq/kg de produit"}}}


def time_snapshot():
    """Minimal pinned baseline for instruction-only (time) drafts."""
    from recipe_wrangler.services.adaptation.models import ComparisonSnapshot
    evidence = dict(reference="test-fixture", method="fixture", version="1", status="stated")
    return ComparisonSnapshot(recipe_id="time-fixture", run_id="run", nutrition_source="irish", release_id="release",
        quantity_input_hash="quantities", serves=2, ingredients=[
            dict(position=0, name="potatoes", original_text="500 g potatoes",
                 quantity=dict(weight_g=500., basis="raw_edible", evidence=evidence)),
            dict(position=1, name="dressing", original_text="50 g dressing",
                 quantity=dict(weight_g=50., basis="as_purchased", evidence=evidence))])
